# 领域术语表（冻结词汇）

状态：frozen（P0.1 冻结；P2 追加房态、员工与账号、发票、投诉、审核、幂等词汇；P7 追加运行报表与健康面板指标口径 §15）
日期：2026-09-27（P2 增补；2026-09-29 P7 增补）
用途：三端 UI、后端实现、接口契约、测试用例与数据报表使用同一套词汇。**英文枚举值是数据库与跨系统契约的取值**，中文术语用于文档与界面文案。
维护规则：新增或变更状态取值，先改本表 → 同步 [核心服务端设计](./design.md) 与 [外部系统接口契约 v0](./contracts/external-systems-v0.md) → 再改实现与测试。落地阶段列标注实现责任阶段（P4–P7）。

## 1. 酒店业务对象

| 术语 | 标识 | 定义 | 落地阶段 |
| --- | --- | --- | --- |
| 房型 / 房间 | `room_types` / `rooms` | 房型、房价、房间档案与门锁设备号；房态见 §11 | P2 / P4 |
| 门锁凭证 | `lock_credentials` | 房间密码的哈希、生效窗口、轮换状态与来源 | P4 |
| 订单 | `orders` | 预订、改期、取消、续订、支付状态、入住与退房时间 | P4 |
| 清洁派单 | `housekeeping_tasks` | 退房清洁与呼叫清洁的派单、执行状态与留证 | P5 |
| 机器人配送 | `robot_deliveries` | 物品配送任务与状态 | P5 |
| 打卡记录 | `attendance_checkins` | 保洁上下班打卡与 AI 审核结果 | P5 |
| 发票 / 投诉 | `invoices` / `complaints` | 发票申请与投诉处理记录 | P4 / P6 |
| 客人 / 员工 | `guests` / `staff` | 微信身份（openid）与员工账号（工号） | P2 / P5 |
| 客人记忆 | `guest_memories` | 客人偏好记忆，含敏感标记 | P7 |

## 2. 订单状态机（`orders.status`）

| 状态 | 含义 | 允许迁移 |
| --- | --- | --- |
| `created` | 已创建（意向单，未进入支付） | → `pending_payment`、`cancelled` |
| `pending_payment` | 待支付 | → `paid`、`cancelled` |
| `paid` | 已支付（支付事实已落 PostgreSQL，触发密码下发与入住指引） | → `checked_in`、`cancelled`（退款口径 P4 定稿后补充） |
| `checked_in` | 已入住 | → `checkout_pending` |
| `checkout_pending` | 退房处理中（清洁与改密未闭环） | → `completed` |
| `completed` | 已完成（终态） | — |
| `cancelled` | 已取消（终态） | — |

- **改期 `rescheduled` 与续订 `extended` 不是状态**，是挂在订单上的事件记录；改期不改订单状态，续订保持当前凭证有效（§3）。
- 支付回调先提交 `paid` 事实再投递 worker（验收主张 A3）；乱序回调不允许回退已 `paid` 状态。
- 门锁调用失败时订单不得进入 `completed`（A1 负例，见契约 §8-4）。

## 3. 门锁凭证生命周期（`lock_credentials`）

| 术语 / 取值 | 含义 |
| --- | --- |
| `next` | 每日轮换预生成的次日凭证（尚未生效，仅存在于轮换流程） |
| `active` | 生效中；同房间同一时刻至多一条；`valid_to` 到期即视为失效（不单独落 `expired` 状态） |
| `revoked` | 已吊销（轮换、退房改密、泄露处置后即时生效） |
| `version` | 同房间单调递增，每次签发/轮换 +1；轮换与吊销需带期望版本，不匹配返回 409 |
| `code` | 明文密码，**仅存在于门锁设备与签发响应载荷**；服务端只存 `code_hash` 与生效窗口 |

轮换来源 `reason`：

| 取值 | 场景 | 行为 |
| --- | --- | --- |
| `checkin` | 首次入住签发 | 生成新凭证 |
| `daily` | 每日轮换 | 到点把预生成的次日凭证转正（旧凭证即刻 `revoked`）并预排下一日；当日凭证缺位或窗口错位时兜底签发 |
| `renewal` | 续订 | 默认保持当前凭证（只展期 `valid_to`，不换码、不换版本）；`reason=renewal` 仅用于兜底签发 |
| `checkout` | 退房 | 保洁确认改密后立即轮换，旧凭证即时吊销 |
| `leak` | 泄露处置 | 店长手动吊销并触发一次轮换（保持原生效窗口，同时清空 `next` 槽位） |

每日轮换口径（P4.7 落地）：

| 项 | 取值 |
| --- | --- |
| 提交时刻 | 酒店本地 00:05 起（cron 全天每 5 分钟探测；去重键 `lock.rotate_daily:{date}` 保证每天至多提交一次） |
| 当日窗口 | `[今日 00:00, 明日 00:00)`（酒店本地时区，`valid_from` 即当日零点） |
| 次日窗口 | `[明日 00:00, 后日 00:00)`，以 `next` 预生成，到点转正 |
| 跳过口径 | 房态 `maintenance`；房态 `occupied`；存在住宿存续订单（`paid` / `checked_in` / `checkout_pending` 且 `check_out_date ≥ 当日`）的房间——在住与在途客人手里的码不动 |
| 幂等 | 同日重跑为空转（不触达设备）；漏跑日按当日窗口自愈（过期 `next` 作废重排） |
| 失败日 | 任务失败关闭、不自动重放（避免设备长时间不可达时无限重试）；人工经 `POST /api/admin/lock/rotation/run` 重跑 |

## 4. 清洁派单状态机（`housekeeping_tasks.status`）

| 状态 | 含义 |
| --- | --- |
| `pending` | 已派单待接单 |
| `accepted` | 保洁已接单 |
| `in_progress` | 执行中 |
| `done` | 清洁完成，待改密确认 |
| `verified` | 已确认密码修改（退房清洁的硬门槛） |
| `closed` | 闭环（终态） |
| `escalated` | 超时未确认的升级态：通知店长人工处置，任务不得进入 `closed` |

- 任务类型：`checkout_clean`（退房清洁；当前/目标密码由门锁服务在执行时下发，落库 payload 与 evidence 不含明文，见 A2）、`call_clean`（呼叫清洁）。
- 留证：照片/视频 + 清洁说明；退房清洁在 `verified` 步骤要求保洁确认已完成密码修改。
- 门锁调用失败时任务不得进入 `closed`（契约 §8-4）。

## 5. 机器人配送状态机（`robot_deliveries.status`）

`queued` → `dispatching` → `delivering` → `arrived` → `delivered`；任意执行态可转 `failed`（附原因）。`delivered` 与 `failed` 为终态；`failed` 不自动重派，由服务层补派新单或转人工（契约 §8-6）。

## 6. 保洁打卡（`attendance_checkins`）

| 术语 | 含义 |
| --- | --- |
| 上班打卡 / 下班打卡 | 两种打卡动作，均上传照片 |
| `face_score` | vision-api 人脸 1:1 比对得分 |
| `badge_ok` | 工牌 OCR 校验结果 |
| `uniform_ok` | 工服检测结果 |
| 审核通过 | 打卡成功入账 |
| 人工复核 | 未通过检测时进入的队列，通知店长；**不存在静默通过路径** |

## 7. Agent 运行时词汇（P1 已实现）

| 术语 | 定义 |
| --- | --- |
| 线程 Thread | 会话线程，FIFO 队列单位按 (用户, Agent, 线程) 划分 |
| 消息 Message | 对话消息记录，先落库再派发 |
| 请求 Request | 一次接入请求；`request_id` 幂等。状态：`queued` → `dispatched` → `finished` / `cancelled` |
| 运行 AgentRun | 请求的执行体。状态：`pending` → `running` →（`cancel_requested` → `cancelled`；`interrupted` 可恢复）；终态：`completed` / `failed` / `cancelled` |
| 运行尝试 attempt | 每次认领一条不可变事实（`attempt_no` + `owner_token`），用于诊断崩溃与租约过期。`outcome` ∈ `completed` / `failed` / `cancelled` / `interrupted` / `lease_expired` |
| 租约 lease 与心跳 heartbeat | 运行中 Run 由唯一 `owner_token` 持有租约，心跳续租；租约过期由收敛任务置 `failed(worker_lease_expired)` |
| 队头 FIFO | 只有 (用户, Agent, 线程) 队首的 `queued` 请求创建 Run；同一线程同时只有一个活跃 Run |
| 投递 dispatch | ARQ 投递必须发生在数据库提交之后；启动/周期补发（repost）只重投 `pending` |
| 运行事件 | `run.started`、`message.delta`、`tool.call`、`tool.result`、`message.completed`、`run.completed`、`run.failed`、`run.cancelled`、`handoff.requested`、`interrupt` |
| SSE 游标 | `ms-seq`（Redis Stream 传输游标，配 `Last-Event-ID`）与 `projected-N`（数据库状态投影回退游标） |
| 审计事实 | `model_audit` / `tool_audit`：模型与工具调用的来源键、顺序、状态、耗时、用量与错误；**报表与评估的唯一权威来源**（A4） |
| 转人工 handoff | 运行中触发人工接线的行为；产生 `handoff.requested` 事件，由管理后台「人工接线」闭环（P7） |

## 8. Agent 与工具

三个内置 Agent：`front-desk`（前台接待，C 端）、`housekeeping-assistant`（员工助手）、`qa-auditor`（质检员）。

| 分组 | 工具 |
| --- | --- |
| 咨询 | `query_kb`、`list_kbs` |
| 房型 | `search_room_types`、`check_availability` |
| 订单 | `create_order`、`reschedule_order`、`cancel_order`、`renew_order`、`apply_invoice` |
| 客房 | `request_room_service`（buy 分支呼叫机器人配送，clean 分支创建保洁派单） |
| 门锁 | `get_checkin_credential`、`rotate_room_credential` |
| 退房与投诉 | `initiate_checkout`、`submit_complaint`、`recommend_nearby` |

副作用工具（订单、门锁类）经 ToolGuard 注入幂等键，并在执行处以当前客人、房间、订单权限 fail-closed 复验（A6）。

## 9. 验收主张

| 编号 | 主张 | 取证阶段 |
| --- | --- | --- |
| A1 | 三端主链路闭环（支付→密码、清洁→改密留证、打卡审核结论） | P4 / P5 / P6 |
| A2 | 密码明文不落数据库、不进记忆与知识库，审计与导出打码 | P4 |
| A3 | 外部事件先提交 PostgreSQL 事实再投递，重复回调不产生重复副作用 | P4 / P5 |
| A4 | 可视化指标由 PostgreSQL 审计事实派生（终态分布、解决率、转人工率、token、召回率） | P7 |
| A5 | 数据中心闭环产生可比较的新旧评估分数，内容变更后旧基线作废 | P8 |
| A6 | 授权在后端依赖与 repository 最终执行，跨端越权被拒绝 | P2 |

## 10. 指标口径

完整报表口径见 [核心服务端设计](./design.md#运行可视化)。易混淆项的正确定义：

- **缓存命中率**：模型提示词缓存（Provider prompt caching）命中的 prompt tokens ÷ 总 prompt tokens，按 Agent、模型与日期聚合；用于评估延迟与成本节省，数据源为 `model_audit` 用量字段。
- **解决率**：一次解决（未转人工、终态 `completed`、无后续同类问题）与任务成功率（订单成功数 ÷ 创建数）分开统计，不混用。
- **召回率**：离线评估集 `recall@k`；线上统计检索片段被最终回答引用的比例，两者分列。

## 11. 房态与账号词汇（P2 追加）

房态 `rooms.status`：

| 取值 | 含义 |
| --- | --- |
| `available` | 可预订 |
| `occupied` | 已入住 |
| `cleaning` | 待清洁 |
| `maintenance` | 维修中 |

接入身份角色 `users.role`：

| 取值 | 含义 |
| --- | --- |
| `customer` | 客人（链接 `guests` 档案） |
| `staff` | 保洁员工（链接 `staff` 档案） |
| `admin` | 管理员（管理端全权，基础数据与账号的唯一写方） |
| `manager` | 店长（管理端全局只读 + 运营处置，见 P7 权限矩阵） |
| `operator` | 客服（会话监控与人工接线，见 P7 权限矩阵） |

账号与档案状态 `users.status` / `staff.status`：`active` / `disabled`；禁用即失去全部会话效力（每次请求回读状态）。

## 12. 其他状态取值（P2 追加）

| 对象 | 取值 |
| --- | --- |
| 发票 `invoices.status` | `requested`（已申请）→ `issued`（已开具）/ `cancelled`（已取消） |
| 投诉 `complaints.status` | `open`（待处理）→ `processing`（处理中）→ `resolved`（已解决）→ `closed`（已关闭） |
| 打卡 `attendance_checkins.direction` | `in`（上班打卡）/ `out`（下班打卡） |
| 打卡审核 `attendance_checkins.review_status` | `passed`（审核通过）/ `manual_review`（人工复核，无静默通过路径） |
| 清洁任务类型 `housekeeping_tasks.task_type` | `checkout_clean`（退房清洁）/ `call_clean`（呼叫清洁） |

## 13. 幂等词汇（P2 追加）

- **幂等键**：`业务对象号 + 操作名`（如 `order_no:create`、`task_id:rotate`），由调用方生成并随请求传递。
- **幂等域 scope**：`idempotency_records.scope` 的取值约定为「对象.动作」（如 `order.create`、`housekeeping.dispatch`、`lock.rotate`）；`(scope, key)` 唯一。
- **重复提交**：命中既有记录时返回既有结果（`result_type` + `result_ref` 指向的事实行），不产生第二次副作用（验收主张 A3）。
- 具备自然唯一键的对象（`orders.order_no`、`lock_credentials(room_no, version)`）在列内以唯一约束兜底，通用去重走 `idempotency_records`。

## 14. 知识库词汇（P3 追加）

文档状态机 `kb_documents.status`（合法迁移的唯一口径见 `app/domain/states.py`）：

| 取值 | 含义 |
| --- | --- |
| `uploaded` | 原件已入 MinIO，等待解析 |
| `parsing` | 解析中（已抢占，持有 claim） |
| `parsed` | 解析完成，分块产物待索引 |
| `indexing` | 索引中（已抢占，持有 claim） |
| `indexed` | 分块正文与向量均已就绪，可被检索 |
| `error_parsing` | 解析失败，可重试抢占 |
| `error_indexing` | 索引写入失败（含 Milvus 双写补偿落点），可重试 |

- **在途状态**：`parsing` / `indexing`。在途状态不是任何抢占的候选集合，保证同一文档同时只有一个 worker 写入。
- **抢占（claim）**：条件 UPDATE 从候选状态集合迁移一次；`claim_owner` 为唯一 attempt token，`claim_expires_at` 为租约到期。解析候选 = `uploaded` / `error_parsing` / `indexed`（`indexed` 支持重建索引）；索引候选 = `parsed` / `error_indexing`。
- **终态写入守卫**：`parsed` / `indexed` / `error_*` 的写入必须持有当前 `claim_owner`，落库即释放租约；失联 worker 的迟到写入不命中任何行。
- **claim 过期**：恢复路径把过期的在途行置 `error_parsing` / `error_indexing`（`error = claim_expired`），等待下一次抢占重试；不自动重放未知外部副作用。
- **文档去重**：`(kb_id, checksum)` 唯一，同库同内容重复上传被拒。
- **可见范围 `knowledge_bases.scope`**：`guest`（C 端可检索）/ `staff`（员工端）/ `internal`（仅内部）；检索工具在执行处复验，不依赖提交时校验。

## 15. 运行报表与健康面板指标口径（P7 追加）

报表（P7.4）、智能体日志（P7.5）与运行健康面板（P7.9）的一切数值都由 PostgreSQL 审计事实（`model_audit`、`tool_audit`、`agent_runs`、`agent_run_attempts`、`requests`、`threads`、`messages`、`message_feedback`、`handoff_requests`、`kb_eval_reports`）**只读 SQL 聚合派生**，不落任何第二份统计副本。这是验收主张 A4 的取证口径：对同一时间窗，报表数值必须等于直接对审计表重算的结果；篡改或删除中间数据都不虚增、不虚减。

**共通约定**：

| 项 | 口径 |
| --- | --- |
| 时间窗 | 所有指标接受 `[start, end)` 半开区间过滤，默认按审计行的 `created_at`（`agent_runs` 用 `created_at`）；分组维度支持按日（酒店本地时区）、`agent_slug`、`model` |
| 软删与报表 | 智能体日志的批量删除是**软删**（`model_audit.deleted_at` / `tool_audit.deleted_at` 置位 + 一条管理操作留痕），只影响日志列表可见性；**报表与健康面板仍按全量审计事实重算（含软删行）**，保证删除中间数据不改变指标（A4） |
| 明文 | 日志详情与列表展示沿用 CredentialGuard 打码后的 `args_digest`，任何指标与导出都不含密码明文（A2） |
| 授权 | 报表、日志、健康面板读接口为 `admin` / `manager`；日志详情可跨用户查看（走角色授权而非线程归属校验），与客人侧 `GET /threads/{id}/audits` 的归属校验区分 |

**报表指标（P7.4）**：

| 指标 | 计算口径 | 数据源 |
| --- | --- | --- |
| 接待量 | 时间窗内会话数（`threads`）、Run 数（`agent_runs`）、消息数（`messages`）分别计数 | threads / agent_runs / messages |
| 解决率（一次解决） | 终态 `completed` 且该线程无 `handoff_requests` 且该 Run 关联助手消息无 `down` 反馈的 Run 数 ÷ 终态 Run 数 | agent_runs / handoff_requests / message_feedback |
| 解决率（任务成功率） | Durable Task 终态 `succeeded` 数 ÷（`succeeded`+`failed`）数；与「一次解决」分列不混用 | tasks |
| 转人工率 | `handoff_requests` 行数 ÷ 会话数（`threads`） | handoff_requests / threads |
| 热门问题 | **降级口径（P7 冷启动）**：高频工具调用 TOP（`tool_audit.tool_name` 计数）与高频首条客人消息关键词 TOP（对每线程首条 `role='user'` 消息做关键词/分词计数）；真实意图聚类 TOP 待 P7.3 qa-auditor 意图打标与 P7.10 标注工作台落地后替换 | tool_audit / messages |
| 召回率（离线） | 最近一次 `kb_eval_reports` 的 `recall@k` 基线 | kb_eval_reports |
| 召回率（线上引用率） | 最后一次模型调用 `retrieval_refs` 非空的 Run 数 ÷ 有检索行为的 Run 数 | model_audit.retrieval_refs |
| token 消耗 | `sum(prompt_tokens)`、`sum(completion_tokens)`、`sum(cached_prompt_tokens)`，按 agent/model/日期聚合 | model_audit |
| 缓存命中率 | `sum(cached_prompt_tokens) ÷ sum(prompt_tokens)`；成本节省 = 命中 tokens × (标准输入单价 − 缓存输入单价)，单价为配置项 | model_audit |
| 工具成功率 | `tool_audit.status='completed'` 数 ÷ 全部工具调用数 | tool_audit |
| 模型重试率 | `agent_run_attempts` 中 `attempt_no > 1` 的 Run 数 ÷ 有尝试事实的 Run 数（一次运行多次认领即重试） | agent_run_attempts |
| 首 token 延迟 TTFT | `agent_runs.first_token_at − agent_runs.started_at` 的 P50 / P95；见下方埋点口径 | agent_runs |

**首 token 延迟（TTFT）埋点口径**：`agent_runs.first_token_at` 在该 Run **首个 `message.delta` 事件**发出时写一次（幂等：仅当列为 `NULL` 时置为当前时间），由运行事件发射器的一次性钩子触发。TTFT = `first_token_at − started_at`（`started_at` 为认领时刻）。流式模型在首 token 回调时落点；非流式模型（DevStub 与测试替身）在整段回退增量时落点，其 TTFT 近似等于整次调用耗时——报表按此口径展示，不区分标注。无 `first_token_at` 的 Run（未产生任何增量即失败/取消）不计入 TTFT 分位。

**运行健康面板指标（P7.9）**：Run 终态分布（`agent_runs` 按 `status` 计数：`completed`/`failed`/`cancelled`）、interrupt 比例（`status='interrupted'` 或 attempt `outcome='interrupted'` 的 Run 占比）、工具成功率、模型重试次数（`agent_run_attempts` 计数）、TTFT P50/P95、缓存命中率趋势（按日序列）。健康面板与报表共用聚合服务层，区别只在呈现窗口（近窗口趋势 vs 业务时间范围）。

**智能体日志（P7.5）**：每条日志画像 = 一次模型或工具调用的审计行，含时间、客人（经 `agent_runs.user_id` → `users`/`guests`）、内容摘要（打码 `args_digest` 或消息文本）、消耗 token、召回引用数；详情 = 单 Run 的 model+tool 合并时间线（复用 `AuditRepository.list_timeline`，走管理端角色授权）；批量删除 = 软删并写管理操作留痕（谁、何时、删了哪些审计行 id），软删行默认不出现在日志列表，但仍在报表重算范围内。
