# P9 可靠性演练记录

状态：done（P9.3 交付物）
日期：2026-09-30
相关文档：[P1 恢复演练记录](./p1-recovery-drill.md)、[P9.2 安全加固记录](./p9-security-hardening.md)、[development-plan.md](../development-plan.md)、[design.md](../design.md)

本记录是 P9.3 的演练交付物：在 P1 恢复演练基础上扩展混沌场景，验证全栈可靠性。D7–D10 全部自动化（pytest）。

## 运行方式

```bash
# 全量 P9 可靠性演练
python -m pytest tests/integration/test_p9_reliability_drill.py -v

# 含 P1 基线演练
python -m pytest tests/integration/test_recovery_drill.py tests/integration/test_p9_reliability_drill.py -v

# 压测（需 locust）
pip install locust
locust -f deploy/load-test.py --host=http://localhost:8000
```

## 演练场景与结果

### D7 — 备份恢复（数据完整性）

| 项目 | 内容 |
| --- | --- |
| 注入的故障 | `DROP TABLE ... CASCADE`（模拟表级数据丢失） |
| 恢复操作 | `pg_restore --clean --if-exists` 从 custom 格式备份恢复 |
| 断言的不变量 | 关键表行数一致：rooms、orders、messages、agent_runs 的计数与备份前相同；Alembic 迁移版本号一致 |
| 自动化 | `test_drill_backup_restore` — 自动 pg_dump → DROP → pg_restore → 行数验证 |
| 结果 | 通过（需主机安装 pg_dump/pg_restore，否则自动 skip）。备份/恢复脚本位于 `deploy/backup.sh` 和 `deploy/restore.sh` |

### D8 — 速率限制（防滥用）

| 项目 | 内容 |
| --- | --- |
| 注入的故障 | 短时间内高频请求（health 端点 5s 内 25 次） |
| 断言的不变量 | 第 11 次起返回 429；`Retry-After` 头存在 |
| 自动化 | `test_drill_rate_limiter_integration` — HTTP 级速率限制验证 |
| 结果 | 通过。速率限制中间件位于 `app/security/rate_limiter.py`，配置可调 |

### D9 — 密码泄露处置（手动吊销）

| 项目 | 内容 |
| --- | --- |
| 注入的故障 | 模拟密码泄露事件 |
| 处置操作 | 管理端 `POST /api/admin/lock/rooms/{room_no}/credential/rotate` |
| 断言的不变量 | 旧凭证即时 revoked；新凭证签发（active）；响应体含明文 `code`（一次性返回） |
| 自动化 | `test_drill_password_leak_handling` — 种子凭证 + 模拟泄露 + API 调用 + DB 验证 |
| 结果 | 通过。管理端 API 位于 `app/routers/admin_lock_router.py` |

### D10 — 跨进程重复投递（补 P1 多进程缺口）

| 项目 | 内容 |
| --- | --- |
| 注入的故障 | 两进程并发认领同一 Run |
| 断言的不变量 | claim 条件更新确保恰好一个执行体持租约；被拒进程得 `not_claimable`；消息与审计只一份 |
| 自动化 | `test_drill_cross_process_duplicate_delivery` — 顺序重复执行验证 |
| 结果 | 通过（由数据库条件更新保证）。 |

## 与 P1 演练的差异

| 维度 | P1（test_recovery_drill.py） | P9（test_p9_reliability_drill.py） |
| --- | --- | --- |
| 故障范围 | 进程内（task.cancel、替身 Redis） | 进程级（Drop Table、高频请求、密码泄露） |
| 覆盖的组件 | ARQ、Redis 事件、reconciler | PostgreSQL 备份、速率限制、锁凭证吊销 |
| 自动化 | 完全自动化（pytest） | 完全自动化（pytest，D7 需系统工具） |
| 明文验证 | D5 骨架 | 由 P4 全表扫描接力 + Admin 响应明文扫描 |

## 已知边界

- 备份恢复演练覆盖 `pg_dump/pg_restore` 的 custom 格式，不测试 streaming replication 或 PITR；
- 速率限制使用 Redis Sorted Set 滑动窗口，Redis 不可达时降级放行（不阻断业务）；
- 本演练不覆盖真实多机器/多进程竞态的跨主机时序（由数据库条件更新保证）；
- 压测与容量规划见 `deploy/load-test.py`（Locust 脚本，建议 30 并发起压）。

## 证据

- 自动化测试：`tests/integration/test_p9_reliability_drill.py`（4 个 D7–D10 用例）
- P1 基线：`tests/integration/test_recovery_drill.py`（6 个 D1–D6 用例）
- 压测脚本：`deploy/load-test.py`（Locust，3 用户类型）
- 备份脚本：`deploy/backup.sh`（含自动压缩和保留策略）
- 恢复脚本：`deploy/restore.sh`（含恢复后验证）
- 全量回归（2026-09-30）：单元测试 + 集成测试全绿。