# P1 恢复演练记录

状态：done（P1-T10 交付物）
日期：2026-09-27
相关文档：[平台机制基线与取舍](../mechanism-baseline.md)、[development-plan.md](../development-plan.md)、[design.md](../design.md)

本记录是 P1-T10 的演练交付物：以故障注入验证 P1 运行时骨架的恢复不变量（[机制基线 §7](../mechanism-baseline.md)）与 P1 退出标准（[development-plan.md](../development-plan.md) P1）。演练已固化为自动化测试，可随时重放；不是一次性人工演练。

## 运行方式

```bash
cd backend
DATABASE_URL="postgresql+asyncpg://haochen:haochen@localhost:5432/haochen" \
  ./.venv/Scripts/python.exe -m pytest -q tests/integration/test_recovery_drill.py
```

依赖真实 PostgreSQL（集成测试会话级临时库）。Redis 抖动场景使用进程内替身，不依赖真实 Redis。

## 演练场景与结果

| # | 场景 | 注入的故障 | 断言的不变量 | 结果 |
| --- | --- | --- | --- | --- |
| D1 | 杀 worker | 运行中 `task.cancel()`（进程内任务消失），租约 1s 过期后由 reconciler 收敛 | 不伪装完成：租约过期收敛为 `failed(worker_lease_expired)`；attempt 记 `lease_expired` 事实（含完成时间）；请求收敛 `finished`；无回复消息、无模型审计行；被杀运行只留下 run.started 事件；接力队头请求被派发（线程不卡死）；SSE 从数据库投影补齐 run.failed 终态（`projected-` 前缀 + 失败原因） | 通过 |
| D2 | 重复投递（顺序 + 并发） | 同一 Run 先后投递两次、并发投递两次 | claim 是条件更新：恰好一个执行体持租约；顺序两投为 `completed`/`not_claimable`，并发两投同为 `completed`/`not_claimable`；回复消息、模型审计、attempt 各恰好一份；事件链只有一组（重复投递不产生第二组投影） | 通过 |
| D3 | worker 重启补发 | 重启时存在：已完成 Run、他机存活 Run（持有 60s 租约）、两个投递丢失的 pending Run | 补发只覆盖 pending：他机运行中的 Run 不被抢（状态保持 `running`）、已完成 Run 不补发；重复补发幂等（再次入队同一集合），重复执行由 claim 拦下（`completed` → `not_claimable`）；两个丢失线程各恰好一条回复 | 通过 |
| D4 | Redis 抖动 | 事件总线 publish / read / follow 全部抛错 | 发布失败不影响运行：Run 照常 `completed`、模型审计照常落库；SSE 退回数据库状态投影，输出合成的 message.completed 与 run.completed（带 `projected` 标记与已提交回复文本） | 通过 |
| D5 | 明文扫描骨架 | 真实 Run（工具循环）全链路产物扫描 | 扫描器可命中 password / id_card / phone 三类明文（正向对照）；Run 的消息内容、模型审计、工具审计参数、事件负载零敏感命中 | 通过 |
| D6 | 全链路验收（M1 退出标准） | HTTP 接入 → 执行 → SSE → 审计查询 | 一条消息从客户端→Run→模型→SSE 全链路可达；SSE 含 message.delta / message.completed / run.completed；请求读接口回报 `completed`；审计读接口可见模型条目（`status=completed`、`prompt_tokens > 0`、`cached_prompt_tokens` 可用） | 通过 |

## 与 P1 退出标准的对照

| 退出标准（development-plan P1） | 覆盖 |
| --- | --- |
| 一条消息从客户端→Run→模型→SSE 全链路可达 | D6 |
| 杀 worker 后 Run 收敛到终态 | D1（收敛 failed 且接力不卡死） |
| 审计行完整可查（验收主张 4「指标由审计派生」的数据基础） | D6（HTTP 读接口）、D5（模型/工具审计行扫描）、D2（副作用只发生一次） |
| 事实先落库与幂等（不变量 1–3） | D2（claim 原子性）、D3（补发只覆盖 pending、他机不被抢） |
| 事件是投影不是事实（不变量 14） | D1（投影补齐终态）、D4（Redis 全故障下事实仍完整） |

## 结论与已知边界

- 恢复语义按设计成立：投递意图（pending）由启动与周期补发兜底；运行中失联由租约收敛为可观察失败；重复投递由 claim 条件更新拦下；事件缺失由数据库投影补齐——四条恢复路径互不依赖（Redis、ARQ、reconciler 各自可单独失效）。
- 不宣称 exactly-once：租约过期一律收敛为 `failed(worker_lease_expired)`，外部副作用是否已发生由 attempt 事实解释，不做自动重放。
- 演练在单进程内注入故障（`task.cancel` 模拟进程死亡、替身总线模拟 Redis 故障），不覆盖真实多进程 / 多机竞态；跨进程正确性由数据库条件更新与租约约束保证，不在本演练断言范围。
- D5 验证的是明文扫描骨架（扫描器 + 零命中断言）；打码与阻断行为随 P4 CredentialGuard 落地，届时以同一套骨架升级为拦截点验证（见机制基线 §9 未决问题 1）。
- 补发与收敛按全库扫描，集成库为会话级共享临时库，因此涉及全库列表的断言用集合包含而非精确计数（用例内有注释说明）。

## 证据

- 测试套件：`backend/tests/integration/test_recovery_drill.py`（6 个用例，D1–D6 一一对应）。
- 全量回归（2026-09-27）：单元测试 81 passed（52 integration 用例另跑）；集成测试 52 passed；`ruff format --check`、`ruff check`、`mypy app` 全绿。
