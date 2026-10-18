# 验收主张取证矩阵

状态：已回填（P9.5 交付物；A9/A10 于 2026-10-18 改造补充）
日期：2026-09-30（A9/A10 更新：2026-10-18）
相关文档：[架构选型决策记录](./2026-09-27-hotel-agent-server-design.md)、[design.md](./design.md)、[development-plan.md](./development-plan.md)

本文档是 P9.5 的交付物：逐条执行 6 条验收主张，回填直接证据与负向案例结果。

---

## A1 — 三端主链路闭环

| 项目 | 内容 |
| --- | --- |
| 主张 | 支付后下发密码与入住指引、清洁派单完成退房改密与留证、打卡审核产出结论 |
| 失败面 | 支付成功未下发密码、任务关闭时密码未轮换、审核失败静默放行 |
| 直接证据 | 与门锁和机器人 mock 的端到端测试：回读订单终态、凭证状态与任务状态 |
| 负向案例 | 注入门锁调用失败与断连：订单不得进入 completed，未确认改密的清洁任务不得 closed |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_checkin_tools.py::test_checkin_flow` | 支付确认后下发密码与入住指引 | 订单状态 `paid`，凭证窗口正确，会话含入住指引 | 通过 |
| 2 | `test_checkin_tools.py::test_delivery_plaintext_never_reaches_persistence` | 下发时明文只存在于 SSE，不落库 | 消息/审计/记忆零明文 | 通过 |
| 3 | `test_lock_rotation.py::test_rotation_after_checkout` | 退房清洁确认改密后凭证轮换 | 旧凭证 revoked，新凭证 active | 通过 |
| 4 | `test_housekeeping_integration.py` | 清洁派单→接单→完成→验证→关闭全链路 | 状态机迁移正确 | 通过 |
| 5 | `test_p4_security_audit.py` | 全表明文扫描 | 7 张业务表零密码明文命中 | 通过 |
| 6 | `test_guest_orders_api.py` | 订单 CRUD 与状态机 | 合法迁移通过，非法拒绝 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 门锁调用失败 | device-sim 注入 lock error | 订单停留在 `paid` 不进入 `completed` | 通过 |
| 2 | 清洁未确认改密 | 跳过确认改密步骤 | 任务不进入 `closed`，超时后 `escalated` | 通过 |

---

## A2 — 密码明文零落库

| 项目 | 内容 |
| --- | --- |
| 主张 | 密码明文不落数据库、不进入长期记忆与知识库，审计与导出对密码字段打码 |
| 失败面 | 密码写入消息历史、记忆表或审计导出 |
| 直接证据 | 集成测试扫描订单、消息、记忆与审计表及导出物 |
| 负向案例 | 构造包含密码的对话与工具回包，断言数据库与导出物无明文 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_p4_security_audit.py::test_p4_plaintext_scan_across_all_tables` | 全表明文扫描 | lock_credentials/tasks/orders/payments/messages/tool_audit/model_audit 零明文 | 通过 |
| 2 | `test_credential_guard.py::test_credential_guard_redacts_plaintext` | CredentialGuard 打码验证 | 工具回包/助手消息/审计入参打码后零命中 | 通过 |
| 3 | `test_recovery_drill.py::test_drill_plaintext_scan_skeleton` | Run 全链路产物明文扫描 | 消息/审计/事件负载零敏感命中 | 通过 |
| 4 | `test_middlewares.py::test_credential_scan_skeleton_detects_plaintext` | 扫描器正向验证 | 可检出三类明文（password/id_card/phone） | 通过 |
| 5 | `test_middlewares.py::test_credential_guard_defaults_to_all_categories_and_rejects_unknown` | 默认打码三类 | 身份证保前 6 后 4、手机号保前 3 后 4、中间等长打码；打码后零命中 | 通过 |
| 6 | `test_credential_mask.py`（8 例） | 部分打码逻辑 | `13812345678`→`138****5678`、身份证→`110101********1234`；订单号等不误伤 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 工具回包含密码 | 构造含密码的工具调用结果 | CredentialGuard 打码后入库内容无明文 | 通过 |
| 2 | 审计入参含密码 | 工具入参含明文密码字段 | `tool_audit.args_digest` 对应字段已打码 | 通过 |
| 3 | 手机号/身份证流经边界 | 工具入参含手机号与身份证 | 默认打码为部分形态；完整串不再命中 `scan_sensitive_plaintext` | 通过 |

---

## A3 — 事实先落库与幂等

| 项目 | 内容 |
| --- | --- |
| 主张 | 外部事件先提交 PostgreSQL 事实再投递 worker，重复回调不产生重复订单或重复派单 |
| 失败面 | 队列消息先于数据库状态、缺少幂等键导致重复副作用 |
| 直接证据 | 真实 PostgreSQL 集成测试：回读状态顺序与幂等键重复提交 |
| 负向案例 | 注入投递失败与重复投递，重试不产生第二条订单或派单 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_idempotency_service.py::test_idempotency_rejects_duplicate` | 幂等键重复提交 | 返回既有结果而非新建 | 通过 |
| 2 | `test_recovery_drill.py::test_drill_duplicate_delivery_executes_once` | 重复投递 | 顺序和并发均恰好执行一次 | 通过 |
| 3 | `test_wechat_payment.py::test_payment_callback_idempotent` | 支付回调幂等 | 重复回调不产生第二单 | 通过 |
| 4 | `test_recovery_drill.py::test_drill_restart_reposts_only_pending` | worker 重启补发 | 只覆盖 pending，已完成不重复 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 重复支付回调 | device-sim 重复投递回调 | 订单只产生一条，不重复扣款 | 通过 |
| 2 | 并发认领同一 Run | 两进程同时认领 | 恰好一个得租约，另一个得 not_claimable | 通过 |

---

## A4 — 指标由审计派生

| 项目 | 内容 |
| --- | --- |
| 主张 | 可视化指标由 PostgreSQL 审计事实派生：终态分布、解决率、转人工率、token 与召回率 |
| 失败面 | 指标取自 Redis 事件或相邻 Run 推断 |
| 直接证据 | 以审计表为源重算指标的集成测试 |
| 负向案例 | 篡改或删除中间数据后指标不得虚增 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_report_service.py` | 报表聚合 | 终态分布/解决率/转人工率/token/recall 全由审计 SQL 派生 | 通过 |
| 2 | `test_admin_reports_api.py` | 报表 API | HTTP 回读指标与审计重算一致 | 通过 |
| 3 | `test_admin_health_api.py` | 健康面板 API | Run 终态分布/TTFT/缓存命中率可查 | 通过 |
| 4 | `test_kb_eval.py::test_eval_report_persisted` | KB 评估报告落库 | 报告从检索配置派生，不落第二份副本 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 删除审计行后指标不变 | 软删 audit 行 | report_service 不查询 deleted_at，报表包含全量行 | 通过（设计约束） |
| 2 | Redis 事件缺失 | 关停 Redis | 报表从 PostgreSQL 直接聚合，不受影响 | 通过（D4） |

---

## A5 — 数据中心闭环

| 项目 | 内容 |
| --- | --- |
| 主张 | 数据中心闭环产生可比较的新旧评估分数，内容变更后旧基线作废 |
| 失败面 | 一边生成 oracle 一边验证、内容变更后沿用旧分数 |
| 直接证据 | 评估运行的集成测试与变更前后实验对比记录 |
| 负向案例 | 修改 FAQ 内容后旧分数标记失效并开新实验 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_kb_eval.py::test_eval_baseline_superseded` | KB 内容变更后基线作废 | 内容指纹变化 → 旧报告标记 superseded | 通过 |
| 2 | `test_eval_agent.py::test_supersede_on_config_change` | 智能体配置变更后基线作废 | 配置指纹变化 → 旧报告标记作废 | 通过 |
| 3 | `test_kb_eval.py::test_eval_metrics_accuracy` | KB 评估指标口径 | recall@K/precision@K 计算正确 | 通过 |
| 4 | `test_eval_agent.py::test_identically_same_run_creates_new_report` | 完全相同配置不触发作废 | 指纹一致 → 新报告追加，旧报告保持有效 | 通过 |
| 5 | `test_p8_eval_quality.py::test_quality_scorecard` | 质量评分卡计算 | 五维评分落库，历史可对比 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 修改 FAQ 内容 | 上传新版本文档 | 旧 KB eval 报告标记 superseded_by + invalidated_reason | 通过 |
| 2 | 评估集不变但配置变化 | 修改检索参数 | 旧 Agent eval 报告标记作废，不可直接比较 | 通过 |

---

## A6 — 服务端授权

| 项目 | 内容 |
| --- | --- |
| 主张 | 授权在后端依赖与 repository 最终执行，跨端越权请求被拒绝 |
| 失败面 | 仅前端隐藏或提示词限制 |
| 直接证据 | 真实 HTTP 负向测试：跨员工任务、跨房间凭证、非管理员配置修改 |
| 负向案例 | 越权直接调用返回拒绝且数据库保持不变 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_authorization_negative.py::test_staff_cross_task` | 跨员工任务越权 | 403 拒绝，数据不变 | 通过 |
| 2 | `test_authorization_negative.py::test_guest_cross_room` | 跨房间凭证越权 | 403/404 拒绝，数据不变 | 通过 |
| 3 | `test_authorization_negative.py::test_non_admin_config_modification` | 非管理员修改配置 | 403 拒绝 | 通过 |
| 4 | `test_authorization_negative.py::test_guest_access_admin` | 客人访问管理端 | 401/403 拒绝 | 通过 |
| 5 | `test_authorization_negative.py::test_staff_access_other_guest_session` | 保洁访问他人会话 | 403 拒绝 | 通过 |
| 6 | `test_auth_flow.py::test_session_expiry` | 会话过期后访问 | 401 拒绝 | 通过 |

**负向取证（14 个负例全部通过，详见 [P2 越权负向取证记录](./drills/p2-authz-drill.md)）：**

| # | 场景 | 断言 | 结果 |
| --- | --- | --- | --- |
| 1-14 | 全部 14 个越权负例（跨员工/跨房间/非管理员/跨端/会话过期） | 401/403/404 | 全部通过 |

---

## A7 — 运行期配置生效

| 项目 | 内容 |
| --- | --- |
| 主张 | 管理端保存的 Agent 配置（`system_prompt`/`model`/`temperature` 与 `enabled_*`）在运行时装配中真正生效 |
| 失败面 | 配置只落库不读、UI 能改运行不生效 |
| 直接证据 | 集成测试：写入配置覆盖后运行 Run，断言模型请求与可见工具集合随之变化 |
| 负向案例 | 未配置覆盖时回退代码声明默认值，行为与改造前一致 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_run_config_wiring.py::test_context_config_override_reaches_model_request` | context 覆盖进入模型请求 | 管理端 `system_prompt` 出现在首个系统消息 | 通过 |
| 2 | `test_run_config_wiring.py::test_enabled_tools_narrowing_limits_visible_tools` | 工具收窄 | 代码声明两工具、管理端启用其一 → 模型只见启用项 | 通过 |
| 3 | `test_agent_config_normalize.py`（4 例） | config_json 形状兼容 | 嵌套 `{context,middleware}` 与旧扁平均可解析；非对象段显式失败 | 通过 |
| 4 | `test_agent_runtime.py`（5 例） | 默认路径回归 | 无配置覆盖时行为不变 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 配置段形状非法 | `context` 段传非对象 | `normalize_config_json` 抛错，不静默 | 通过 |
| 2 | 无配置覆盖 | 不写 `AgentConfig` 行 | 回退代码声明默认值（`enabled_*` 为 None） | 通过 |

---

## A8 — 保洁客评闭环

| 项目 | 内容 |
| --- | --- |
| 主张 | 客人可对已交付的清洁任务评分，反馈读接口不再读不存在的列 |
| 失败面 | 反馈接口读 `housekeeping_tasks.guest_score/guest_comment` 但列从未落地 → 一经真实调用即报错；评分只有读没有写，永远是空集 |
| 直接证据 | 集成测试：评分落库后反馈读接口可捞出；客人端到端评分返回 200 并回显 |
| 负向案例 | 未交付任务不可评（状态守卫）；越界评分被拒；他人任务评分 403；未认证 401 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_housekeeping_rating.py::test_get_staff_ratings_no_longer_errors` | 反馈读接口回归 | 不因缺列报错，能捞取已评任务（含 rated_at） | 通过 |
| 2 | `test_housekeeping_rating.py::test_guest_rates_own_task_end_to_end` | 客人评分端到端 | 200 + 回显 score/comment/rated_at | 通过 |
| 3 | `test_schema_domain.py::test_housekeeping_guest_rating_columns` | 列落地基线 | `guest_score/guest_comment/rated_at` 存在于 housekeeping_tasks | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 任务未交付 | 对 pending 任务评分 | `ConflictError`，状态不变 | 通过 |
| 2 | 评分越界 | score=0 / score=9 | `ValidationFailedError` | 通过 |
| 3 | 评他人任务 | 非本人 token 评分 | 403（归属 fail-closed） | 通过 |
| 4 | 未认证 | 无 token 评分 | 401 | 通过 |

（A8 为 2026-10-13 修复既有潜在 bug 新增：`get_staff_ratings` 读不存在的列。）

---

## A9 — MCP 服务器运行时生效

| 项目 | 内容 |
| --- | --- |
| 主张 | 管理端注册并启用的 MCP 服务器在运行时真实生效：连接测试只发现工具、不执行副作用；启用的工具进入模型可见集合并经统一工具节点审计；单工具可禁用；外部渠道经 `mcp-gateway` 以 `streamable_http` 暴露 |
| 失败面 | MCP 配置只落库不消费、连接测试执行工具产生副作用、未启用工具仍对模型可见、`stdio` 被注入、凭证明文落库、server 不可达拖垮 Run |
| 直接证据 | 客户端/探测单测、运行期装载单测、gateway 三 server 契约测试、真实 PostgreSQL 的运行期集成测试 |
| 负向案例 | `stdio` 拒绝（422）、未启用不可见、`disabled_tools` 剔除、不可达跳过并告警、探测只 `tools/list`、凭证只从环境变量注入 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_mcp_client.py::test_validate_transport_rejects_stdio_and_unknown` | 传输白名单 | `stdio` 与未知传输被拒 | 通过 |
| 2 | `test_mcp_client.py::test_discover_tools_returns_metadata_only` | 探测只发现工具 | 仅返回名称/描述元数据，不调用任何工具 | 通过 |
| 3 | `test_mcp_client.py::test_probe_server_caches_by_config_hash` | 工具对象缓存 | 配置哈希不变复用，变更失效 | 通过 |
| 4 | `test_mcp_runtime.py::test_load_mcp_tools_prefixes_and_marks_dangerous` | 运行期装载 | 工具加 `{slug}__` 前缀、默认 `dangerous=True` | 通过 |
| 5 | `test_mcp_runtime.py::test_read_only_and_disabled_tools` | 单工具治理 | `read_only_tools` 豁免审批，`disabled_tools` 不装载 | 通过 |
| 6 | `test_mcp_runtime.py::test_skips_unreachable_server` | 失败语义 | 不可达 server 跳过并告警，不阻断装配 | 通过 |
| 7 | `test_mcp_gateway.py`（12 例） | gateway 三 server | `/lock`、`/robot`、`/wechat` 的 `streamable_http` 工具可发现；工具转发到渠道客户端；渠道失败收敛为 `{ok:false}` | 通过 |
| 8 | `test_mcp_gateway.py::test_lock_tool_reaches_device_sim_contract` | 全链契约 | MCP 工具 → `LockClient` → device-sim 签发凭证成功 | 通过 |
| 9 | `test_robot_wechat_clients.py`（2 例） | 渠道客户端契约 | 配送下单/回读、支付单状态回读与 404 显式失败 | 通过 |
| 10 | `test_agent_mcp_tools.py::test_enabled_mcp_server_tools_reach_model` | 运行时注入 | 写 `enabled_mcp_servers` → 工具对模型可见 | 通过 |
| 11 | `test_mcp_servers_api.py::test_test_connection_writes_probe_result` | 连接测试落库 | 探测结论写回 `last_status/last_error/last_tested_at/discovered_tools` | 通过 |
| 12 | `test_mcp_servers_api.py::test_seed_registers_gateway_servers_idempotently` | 种子注册 | 空库 seed 后 lock/robot/wechat 三条记录存在且重复 seed 不重复 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 配置 `stdio` 传输 | 创建/更新时传 `transport=stdio` | 422 拒绝，不写入 | 通过 |
| 2 | 未启用 MCP server | 不写 `enabled_mcp_servers` | 工具不出现在模型可见集合 | 通过 |
| 3 | 单工具禁用 | `config_json.disabled_tools` 命中 | 该工具不装载、模型不可见 | 通过 |
| 4 | server 不可达 | 装载期连接失败 | 跳过该 server 工具并告警，Run 继续 | 通过 |
| 5 | 探测副作用 | 连接测试 | 只发 `initialize` + `tools/list`，无 `tools/call` | 通过 |
| 6 | 凭证落库 | 扫描 `mcp_servers` 行与连接描述 | 无 api_key/授权头明文，凭证仅从 `MCP_<SLUG>_API_KEY` 环境注入 | 通过 |
| 7 | 越权管理 | staff/manager 写 MCP 配置 | 403 拒绝；admin 通过 | 通过 |

---

## A10 — 数据管道可靠调度与失败可见

| 项目 | 内容 |
| --- | --- |
| 主张 | 清洗管道每次运行都有确定终态、失败可见；导出按 checkpoint 增量、归档按真实主键推进；规则开箱可用且 cron 由 task-worker 真调度（分钟级去重）；数据湖与管理端指标可查；报表预聚合陈旧时自动回落 PostgreSQL，不再静默归零 |
| 失败面 | Run 永卡 `running`、失败被伪装为 `completed`、checkpoint 键不匹配导致每轮全量重导、同规则并发重叠、`schedule_cron` 只存不执行、陈旧预聚合分片让指标静默归零 |
| 直接证据 | 真实 PostgreSQL 集成测试（终态/归档/事务/互斥/调度）、handler 单测、报表降级单测、管理端数据湖页构建 |
| 负向案例 | 预聚合抛错必须落 `failed`、同规则并发第二次拒绝、同一分钟重复调度不重复提交、非法 cron/task_type 保存即 422、陈旧分片触发 PG 回落 |

**证据清单：**

| # | 测试用例 | 描述 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `test_data_pipeline_reliability.py::test_build_aggregates_failure_writes_failed_terminal` | 预聚合失败终态 | Run 落 `failed` 且 `error` 留痕 | 通过 |
| 2 | `test_data_pipeline_reliability.py::test_archive_audit_logs_archives_by_real_primary_key` | 归档主键 | `tool_audit` 按真实 `id` 归档并软删主表，Run `completed` | 通过 |
| 3 | `test_data_pipeline_reliability.py::test_trigger_commits_run_and_task_together` | 事务竞态回归 | run 行与 task 行同事务可见，提交后投递一次 | 通过 |
| 4 | `test_data_pipeline_reliability.py::test_trigger_rejects_concurrent_same_rule` | 并发互斥 | 运行中的同规则第二次触发返回 `None`，仅一条 run | 通过 |
| 5 | `test_data_pipeline_reliability.py::test_scheduler_submits_due_rule_and_dedupes_within_minute` | 真实调度 | 命中规则产生 `scheduled` run；同一分钟重复调用不重复提交 | 通过 |
| 6 | `test_data_pipeline_handlers.py::test_get_checkpoint_reads_by_scope_not_source_table` | 增量导出 | checkpoint 按作用域读取，与 `source_table` 解耦 | 通过 |
| 7 | `test_data_pipeline_handlers.py::test_complete_run_merges_scope_and_checkpoint_into_summary` | 终态摘要 | 完成时将 scope/checkpoint 合并进 `summary` | 通过 |
| 8 | `test_data_pipeline_rules_api.py::test_seed_cleaning_rules_idempotent` | 规则种子 | 空库 9 条默认规则，重复 seed 不重复插入 | 通过 |
| 9 | `test_pipeline_scheduler.py`（5 例） | cron 判定 | 每分钟/定点/步长命中正确；非法 cron 不抛；去重键按分钟 | 通过 |
| 10 | `test_report_service_degrade.py`（4 例） | 报表新鲜度守卫 | 默认走 PG；湖不可达/分片陈旧回落 PG 且不归零；新鲜分片走快路径 | 通过 |
| 11 | `admin-web` `type-check` + `build` | 数据湖前端页 | 分区浏览/预览/只读 SQL/房型月度指标可构建，湖不可达显式降级 | 通过 |

**负向取证：**

| # | 场景 | 注入 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 预聚合抛错 | lake 不可达 | Run 终态 `failed`，带 `error`，不再静默返回 | 通过 |
| 2 | 失败伪装成功 | 归档表级异常 | 整体 `failed` 并留痕，不再报 `completed` | 通过（代码路径 + 终态测试） |
| 3 | 同规则并发 | 连续两次触发 | 第二次拒绝，不产生孤儿 run | 通过 |
| 4 | 调度重复 | 同一分钟两次扫描 | 第二次提交数为 0 | 通过 |
| 5 | 非法任务/表达式 | 未知 `task_type` / 非法 cron | 保存即 422，运行期不静默跳过 | 通过 |
| 6 | 陈旧预聚合分片 | 分片早于窗口 | 指标回落 PG 重算，结果与审计重算一致（不归零） | 通过 |

---

## 汇总

| 验收主张 | 状态 | 正例数 | 负例数 | 关键证据 |
| --- | --- | --- | --- | --- |
| A1 三端主链路闭环 | ✅ 通过 | 6 | 2 | 端到端测试 + 状态机验证 |
| A2 密码零明文 | ✅ 通过 | 4 | 2 | 全表扫描 + CredentialGuard 测试 |
| A3 事实先落库与幂等 | ✅ 通过 | 4 | 2 | 幂等 + 恢复演练 |
| A4 指标由审计派生 | ✅ 通过 | 4 | 2 | 报表服务 + API 集成测试 |
| A5 数据中心闭环 | ✅ 通过 | 5 | 2 | KB/Agent/评分卡评估测试 |
| A6 服务端授权 | ✅ 通过 | 6 | 14 | 越权负向测试全部通过 |
| A7 运行期配置生效 | ✅ 通过 | 4 组 | 2 | 配置接线集成测试 + 形状兼容单测 |
| A8 保洁客评闭环 | ✅ 通过 | 3 | 4 | 评分列落地 + 端到端评分 + 归属/状态守卫 |
| A9 MCP 服务器运行时生效 | ✅ 通过 | 12 | 7 | 探测/装载/网关契约 + 运行期可见集合 + 种子注册 |
| A10 数据管道可靠调度与失败可见 | ✅ 通过 | 11 | 6 | 终态/归档/事务/互斥/调度 + 报表降级 + 前端构建 |

**P9.5 结论**：原 8 条验收主张（A1–A8）均有直接证据（正例 38 个 + 负例 31 个）。2026-10-18 改造补充 A9（MCP 运行时）与 A10（数据管道可靠性），当前矩阵共 **10 条主张全绿**（正例 61 个 + 负例 44 个）；A9/A10 的 23 个正例与 13 个负例均有可复跑用例（单测 / 集成 / 前端构建）。