# 平台机制基线与取舍

状态：draft（P1–P2 落地基准）
日期：2026-09-27
相关文档：[design.md](./design.md)、[2026-09-27-hotel-agent-server-design.md](./2026-09-27-hotel-agent-server-design.md)、[development-plan.md](./development-plan.md)、[外部系统接口契约 v0](./contracts/external-systems-v0.md)、[P1 恢复演练记录](./drills/p1-recovery-drill.md)、[P2 越权负向取证记录](./drills/p2-authz-drill.md)

本清单逐机制给出采用方式（直接采用 / 改造 / 新建 / 裁剪）、落地位置、酒店域增量与必须保留的不变量，作为 P1 运行时骨架、P2 领域与授权基座与后续阶段（P3–P9）的实现基准。落地位置以 `backend/app/` 下的实际骨架为准；与实现冲突时，先更新本清单并在 PR 中说明。

## 1. 采用方式总览

| # | 机制 | 采用方式 | 落地位置 | 阶段 |
| --- | --- | --- | --- | --- |
| 1 | 分层边界（薄路由 / services / repositories） | 直接采用 | backend/app/routers、services、repositories | P0–P2 |
| 2 | Request/Run 两阶段 + 线程 FIFO | 直接采用 | app/services/agent_request_queue_service.py、app/services/run_worker.py | P1 |
| 3 | Run 七态 + lease/heartbeat + attempt 事实表 | 直接采用 | app/repositories/agent_run_repository.py | P1 |
| 4 | 恢复与 reconciler（pending 重投递、completed 接力） | 直接采用 | app/services/run_worker.py | P1 |
| 5 | Durable Task（tasks 表 + handler registry + claim/lease） | 直接采用（换领域 Handler） | app/services/task_service.py、app/services/task_registry.py | P1 骨架 / P4–P8 Handler |
| 6 | BaseAgent 装配与运行时上下文 | 直接采用 | app/agents/base.py、app/agents/context.py | P1 |
| 7 | 中间件流水线 | 改造（12 → 11 道，见 §3） | app/agents/middlewares/ | P1 主体 / P4–P5 新增 |
| 8 | 工具系统（三层注册 + 授权分离） | 直接采用 | app/agents/toolkits/registry.py | P1 / P3–P5 |
| 9 | Skills（SKILL.md + 渐进/预加载 + 依赖） | 直接采用 | app/agents/skills/ | P3–P5 |
| 10 | MCP 集成与网关 | 改造（远程 MCP 承载 mcp-gateway） | app/mcp_gateway/ | P0 mock / P4–P5 真机 |
| 11 | 知识库（状态机 + 双写 + 权限） | 直接采用 | app/knowledge/ | P3 |
| 12 | 上下文压缩（确定性压缩 + 摘要） | 直接采用 | app/agents/middlewares/summary.py | P1 |
| 13 | 审计（model_audit / tool_audit + 统一读接口） | 直接采用（字段加酒店域） | app/models、app/repositories、audit 读接口 | P1 写入 / P7 界面 |
| 14 | 运行事件流与状态投影（SSE） | 直接采用（补强：事件流按 Run 隔离 + 数据库终态兜底，见 §2.14） | app/services/run_event_bus.py、app/services/run_events.py、app/services/run_stream.py | P1 |
| 15 | Langfuse（预创建 trace 固化 + 反馈 score） | 直接采用（可选组件） | app/services/langfuse_service.py | P8 |
| 16 | 记忆（MEMORY.md + 开关 + 敏感管控） | 改造（酒店记忆表 + 敏感标记） | app/agents/middlewares/memory.py | P7 |
| 17 | 双评估（知识库评估 + Langfuse Dataset 实验） | 直接采用（重建评估集） | app/eval/ | P3 基线 / P8 闭环 |
| 18 | 用户定时 Agent | 裁剪（定时需求走领域 Durable Task） | — | — |
| 19 | SubAgents / 沙盒 / 多租户 / 只读连接器 / 产物工作台 | 裁剪（见 §4） | — | — |

## 2. 逐机制详表

### 2.1 分层边界与请求接入

| 项 | 内容 |
| --- | --- |
| 机制要点 | 路由只做认证与输入校验；用例在 service；持久化在 repository。请求先在 PostgreSQL 持久化 Message + Request，再按 (用户, Agent, 线程) FIFO 派发；只有队头 ready 请求创建 AgentRun；**ARQ 投递发生在数据库提交之后**；`request_id` 复用返回已有 Request/Run，跨用户/Agent/线程复用返回冲突 |
| 三档队列策略 | `enqueue`（普通聊天，可查排队位置、派发前取消）、`reject`（同步调用立即拒绝）、`steer`（主会话修正方向，安全点接力）。酒店建议：C 端与保洁端聊天用 `enqueue`；管理端同步触发用 `reject`；Steer 可暂不开放（P1 保留代码位，业务不启用） |
| 酒店域增量 | 三个 Agent 各自线程：front-desk（客人）、housekeeping-assistant（员工）、qa-auditor（离线跑批不走向队列，走 Durable Task）；支付回调与门锁事件是外部事实，走 webhook 薄路由 + 事实先落库 + 投递，不进请求队列 |
| 落地位置 | app/routers/、app/services/agent_request_queue_service.py、app/repositories/ |
| 阶段 | P1（骨架）、P4（支付回调接入） |

### 2.2 Run 生命周期、租约与 attempt 事实

| 项 | 内容 |
| --- | --- |
| 机制要点 | ① 终态写入只接受当前 worker attempt，并清除 lease；② 过期的 running/cancel_requested 收敛为 `failed (worker_lease_expired)`，**不伪装成 exactly-once 重试**（外部副作用可能已发生）；③ 独立 `agent_run_attempts` 表：每次执行一条不可变事实（run_id + attempt_no 唯一、owner token、started/heartbeat/lease_expires/finished、outcome 与 error 分类），reconciler 只收敛未终结 attempt，历史不回填；④ intake/resume/continue/接力在 Conversation 行锁内读写，数据库唯一约束兜底 |
| Run 状态 | `pending`（投递意图已提交）→ `running`（唯一 attempt 持 lease + heartbeat）→ `completed` / `failed` / `cancelled` / `interrupted`（等待回答或审批，resume 恢复）；另有 `cancel_requested`（取消意图已落库，Redis 信号只用于加速感知） |
| 酒店域增量 | 门锁/机器人副作用工具（§2.5）的失败语义与 attempt 事实对齐：门锁调用失败必须能解释"密码是否已下发" |
| 落地位置 | app/repositories/agent_run_repository.py、app/services/run_worker.py |
| 阶段 | P1（验收主张 A3 的顺序断言基础） |

### 2.3 Durable Task

| 项 | 内容 |
| --- | --- |
| 机制要点 | PostgreSQL `tasks` 行拥有持久执行意图、状态、去重键（dedupe key）、`handler_version`、attempt owner、heartbeat 与 lease；API 提交 `task_type + handler_version + payload` 后只发布 `task_id`；发布失败保留 pending，由 worker 启动与周期 publisher 补发；worker 从静态 registry 惰性加载领域 Handler，唯一 attempt token 原子 claim；**数据库 claim 上限 4**，ARQ 槽位由 `ARQ_MAX_JOBS` 配置——长知识任务不能占满 AgentRun 容量；重复投递不能取得运行中任务；失联统一失败（不自动重放未知外部副作用），failure hook 在终态事务内收敛关联文件；未知 handler_version fail-closed |
| 酒店域 Handler | ① 每日密码轮换 `lock.rotate_daily`（P4.7 已交付：cron 全天每 5 分钟探测、酒店本地 00:05 起提交，去重键 `lock.rotate_daily:{date}` 每天至多一次，任意终态都不自动重放，失败日由人工经管理端重跑）；② 清洁任务超时收敛与 escalated（P5.3）；③ 知识库上传解析索引（P3.1，直接采用知识库 Handler）；④ 质检跑批 qa-auditor（P7.3/P8）；⑤ 评估运行（P8）；⑥ 入住凭证签发 checkin.prepare（P4.2/P4.3，支付事实提交后置；P4-T6d 起在同一次执行内完成微信订阅消息第二通道下发，失败不影响任务结果） |
| 落地位置 | app/services/task_service.py、app/services/task_registry.py + 各领域 Handler |
| 阶段 | P1 骨架；Handler 随 P3/P4/P5/P7/P8 落地 |

### 2.4 Agent 装配与运行时上下文

| 项 | 内容 |
| --- | --- |
| 机制要点 | `BaseAgent` 子类（`buildin/<agent>/` 目录 + `__init__.py` 导出，自动发现注册）；`context_schema`（dataclass + metadata）驱动管理端配置表单（get_configurable_items → Agent 详情 → 前端表单 → config_json.context → get_graph）；`prepare_agent_runtime_context` 在 Graph 创建前按当前用户权限归一化资源（工具、知识库、MCP、Skills），**资源快照不是授权替代品**，工具执行处仍复验；运行入口读 AGENTS.md / USER.md（各 64 KiB 上限）追加系统提示词；checkpoint 只用 PostgreSQL；resume 重建 Context，不从相邻 Run 猜测结果 |
| 模型接入 | OpenAI 兼容接口（`MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_TEMPERATURE`，可对接各家兼容网关与自建推理）；模型层不重试，重试归中间件流水线；**未配置 `MODEL_API_KEY` 时回退系统环境变量 `DEEP_SEEK_API_KEY`（compose 直通），仍为空则降级为开发态 stub 模型**（无网络、回复带 `[dev-stub]` 前缀），全链路与测试不依赖外部模型；缓存命中率取模型用量 `input_token_details.cache_read` |
| 酒店域增量 | 三张图：front-desk（客人身份上下文：guest_id、房间、订单）、housekeeping-assistant（staff_id、任务载荷可见范围）、qa-auditor（离线，无聊天入口）；配置表单字段对齐管理模式：提示词、模型、工具权限（按 Agent 授权）、知识库范围、Skills、MCP |
| 落地位置 | app/agents/（base.py、context.py、registry.py、models.py、runtime.py、prompts.py）、buildin/front_desk/、buildin/housekeeping_assistant/、buildin/qa_auditor/；checkpoint 表由 migrations/20260927_0003 建，运行期不建表 |
| 阶段 | P1（骨架 + front-desk 占位）、P3–P5（三图完整） |

资源策略：默认 `DenyAllResourcePolicy`（失败关闭，权限数据落地后替换）。P2–P4 过渡用 `DeclaredBuiltinPolicy`（由 P1 的"仅只读"扩展而来）：只放行 **Agent 代码声明的工具 id 与声明的知识库**，放行集合随代码评审、不由运行期配置扩大；声明了但未注册的工具在装配期显式失败。写类工具（订单变更，P4-T1 落地）在**工具执行处逐次复验**——身份派生客人档案、订单归属、订单状态、日期窗与房态库存，fail-closed。

### 2.5 工具系统（注册与授权分离）

| 项 | 内容 |
| --- | --- |
| 机制要点 | 三层：内置工具（`@tool(category=...)` 注册）、知识库工具（由 `knowledge-base` Skill 激活后对模型开放）、MCP 工具（服务器启用列表 + 单工具禁用）；**"可执行"与"模型可见"分离**：工具先注册进 ToolNode，Skill 激活后才向模型开放 schema；模型看见工具也不代表可访问任意资源——`kb_id`、`file_id`、路径、身份都在执行处复验；工具元数据（display_name 等）由稳定注册信息提供，供管理端展示 |
| 酒店域工具分组 | 咨询：query_kb、list_kbs；房型：list_room_types（在售房型与价格，库存/容量在预订与改期时复验）；订单：list_my_orders、create_order、reschedule_order、renew_order、cancel_order、apply_invoice（副作用，幂等 + 执行处复验；P4-T1 落地）；客房：request_room_service（buy/clean 分流）；门锁：get_checkin_info（只读视图，永不带明文）、deliver_room_password（副作用，按需轮换下发；P4-T6b 落地）；退房与投诉：initiate_checkout、submit_complaint、recommend_nearby |
| 落地位置 | app/agents/toolkits/（registry + 各领域工具模块）、app/mcp_gateway/ |
| 阶段 | P1（registry）、P3–P5（工具逐个落地） |
| 注意 | 门锁/订单类副作用工具的执行处复验对象是**当前客人 + 房间 + 订单**权限，fail-closed |

P1 已落地 `app/agents/toolkits/registry.py`：`@builtin_tool(category=...)` 注册（稳定注册元数据：category / skill / dangerous / read_only / display_name，供门控与管理端展示共用）、ToolNode 装配（执行处复验 Skill 激活集合，另写 tool_audit 事实：状态、耗时、入参摘要；入参打码由 CredentialGuard 在 P4 接管）；并随附一个只读演示工具 `get_current_time`（`toolkits/system.py`，领域工具随 P3–P5 替换）。Agent 的工具意图由 `BaseAgent.tool_ids` 声明（P2 起改由配置表单提供），经资源快照归一化后进入注册表选取：未授权工具既不出现在模型请求里，也不在 ToolNode 中；未激活 Skill 的工具即便被模型点名也拒绝执行。

### 2.6 Skills

| 项 | 内容 |
| --- | --- |
| 机制要点 | `SKILL.md` YAML frontmatter（name、slug、description、tool_dependencies、mcp_dependencies、skill_dependencies）；渐进加载：模型先看到名称/描述/路径，读取 SKILL.md 后 `activated_skills` 生效，才开放其声明的工具与 MCP；**未激活 Skill 的工具即便已注册也不可调用**；预加载（preload_skills）从首轮开放依赖闭包，根文件缺失必须显式失败；个人 Skill 覆盖同名共享 Skill；Agent 的 Skill 选择不能扩大用户权限 |
| 酒店域 Skills | checkin-flow（支付校验、密码下发、入住指引；P4-T6a 已交付内容与装载机制）、checkout-flow（退房、清洁、发票）、housekeeping（保洁流程与留证规范，员工 Agent 专用）、complaint-handling（投诉分级与告警）、invoice（发票流程）。checkin-flow 用预加载（主链路须首轮即可用），其余随落地评估渐进加载 |
| 落地位置 | app/agents/skills/（catalog 装载与校验 + middleware 激活门控）、内容根 `backend/skills/`（容器内 `/srv/app/skills`） |
| 阶段 | P3（机制骨架）、P4（checkin-flow 内容 + 预加载；渐进激活随 P5 首个渐进 Skill 落地） |

### 2.7 MCP 与 mcp-gateway

| 项 | 内容 |
| --- | --- |
| 机制要点 | 管理接口只接受 `streamable_http` 与 `sse` 远程 MCP；`stdio` 仅限代码维护的系统内置项（命令/版本固定，禁止从 HTTP 或数据库拼接）；MCP 配置存 PostgreSQL，工具对象按配置哈希缓存；连接测试只发现工具不执行副作用 |
| 落地方案 | mcp-gateway 作为独立服务，把 lock / robot / wechat-pay / pms 各以 `streamable_http` MCP 暴露（内部网络），经管理链路注册、测试连接、启用与单工具禁用，与此边界一致；Agent 侧不直连硬件 SDK。P0 已交付契约 mock（`app/device_sim`，含故障注入，见[外部系统接口契约 v0](./contracts/external-systems-v0.md)） |
| 落地位置 | app/mcp_gateway/（lock-mcp、robot-mcp、wechat-mcp、pms-mcp） |
| 阶段 | P0（mock）、P4–P5（接线）、P9（真机） |

### 2.8 知识库

| 项 | 内容 |
| --- | --- |
| 机制要点 | 文档状态机 `uploaded → parsing → parsed → indexing → indexed`（另有 error_parsing / error_indexing），`parsing`/`indexing` 为条件抢占，并发不双写；存储分工：PostgreSQL 拥有配置/权限/文件元数据与状态/chunk 正文/Task 意图与 lease，MinIO 拥有原件与解析产物，Milvus 拥有向量与 BM25 字段，Neo4j 拥有实体关系，Redis 只做投递与最小配置缓存；Milvus 索引是 PG + Milvus 双写、非跨存储事务，失败补偿并置 error_indexing；批量入口按状态筛选 + dedupe key 拒绝活跃重复任务；检索工具参数（kb_id/file_id/文件名）执行处复验权限 |
| 酒店域增量 | 知识库内容为酒店介绍、政策、FAQ、WiFi/早餐/退房时间等入住指引来源；图谱用于房型-设施-周边（recommend_nearby）；知识库内容变更触发评估基线作废（P8）。检索落地为两段式：Milvus 混合宽捞（向量 + BM25，`KB_RECALL_LIMIT`=30）→ 重排截断（默认 `KeywordReranker` 按 token 覆盖度打分；`KB_RERANK_ENABLED` 打开时优先 `HttpReranker`（`RERANK_BASE_URL`，BGE cross-encoder），否则 LLM 重排，超时降级保序）→ 上下文装配（`KB_RERANK_TOP_N`=6、`KB_CONTEXT_MAX_CHARS`=3000、每文档 `KB_CONTEXT_MAX_CHUNKS_PER_DOCUMENT`=3 段） |
| 落地位置 | app/knowledge/（manager、base、implementations/milvus、retrieval.py、rerank.py、tokenization.py）、app/routers/knowledge_router.py |
| 阶段 | P3（验收主张 A5 的新鲜度/覆盖率治理对象） |

### 2.9 上下文压缩

| 项 | 内容 |
| --- | --- |
| 机制要点 | 唯一压力阈值 `summary_threshold`（默认 100K，环境变量 `SUMMARY_THRESHOLD`）+ `summary_keep_messages`（默认 10，`SUMMARY_KEEP_MESSAGES`）；先**确定性压缩**（大工具结果写入 Workdir 的 outputs/large_tool_results/，请求中保留路径 + 哈希 + 预览；`LARGE_RESULT_THRESHOLD_BYTES` / `LARGE_RESULT_PREVIEW_CHARS` 控制），同口径重计量仍达阈值才调摘要模型；摘要事件以图状态 `summarization_events`（追加语义）随 checkpoint 累积，最新一条即本次事件（cutoff + 摘要 + `outputs/summaries/` 历史文件路径 + 压缩前后消息数/近似 token）；**PostgreSQL 聊天消息不因压缩删除**；内部摘要模型带 nostream 标签不流给用户，其用量计入 Run 的 token_usage；主动压缩与普通请求共用 Conversation 行锁，忙时 409 thread_busy |
| 酒店域增量 | 无领域改动；注意 CredentialGuard（§3）必须先于摘要与记忆处理明文 |
| 落地位置 | app/agents/middlewares/summary.py（摘要）、filesystem.py（确定性压缩）、pipeline.py（装配）、app/services/context_compression_service.py（P1 后续主动压缩接口） |
| 阶段 | P1 |

### 2.10 审计与观测

| 项 | 内容 |
| --- | --- |
| 机制要点 | 按 LangGraph lifecycle 持久化 Model / Tool 调用审计事实：稳定来源键、严格顺序（seq）、观察时间、执行状态、monotonic 耗时；Model 存 Provider usage（含 cached prompt tokens，供缓存命中率与成本报表）；Tool 存 effective input、原始 output 或 error；终态 State 对账写 `state_reconciled` 证明；**审计行不进入普通 History、Memory 与 Dashboard 统计**；读接口统一为一条线程 Model/Tool 审计时间线，最新 500 条 + `truncated` 标记；sequence 冲突显式失败；同 operation ID 的 Model 与 Tool 记录独立 |
| 酒店域增量 | **字段级扩展**：召回引用（retrieval_refs）用于线上召回率；密码字段打码（CredentialGuard）保证审计不留明文；管理后台"智能体日志"页直接消费该读接口（时间、客人、内容、token、召回率、批量删除软删 + 审计） |
| 落地位置 | app/models/audit.py、app/repositories/、app/routers/chat_router.py（audits） |
| 阶段 | P1（写入）、P7（界面，验收主张 A4） |

### 2.11 Langfuse

| 项 | 内容 |
| --- | --- |
| 机制要点 | 可选组件，未配置不阻断业务；Run 在模型执行前用独立短事务幂等固化预创建 trace ID 到 `AgentRun.langfuse_trace_id`，callback 不得用 `last_trace_id` 改写；映射 uid→user_id、thread_id→session_id；点赞/点踩先落本地业务表（P3 已落 `message_feedback`：同身份同消息唯一、改评即更新、取消即删除），有 trace 再写 user-feedback score（P8）；**PostgreSQL 审计是唯一权威事实**，Langfuse 只是排障视图 |
| 酒店域增量 | 管理后台健康面板与 Run 详情跳 Langfuse trace 作为可选排障入口 |
| 落地位置 | app/services/langfuse_service.py、app/services/feedback_service.py |
| 阶段 | P8 |

### 2.12 记忆与敏感信息

| 项 | 内容 |
| --- | --- |
| 机制要点 | Memory 由开关 + 文件内容双重条件启用；Memory 内容不是系统指令；读取与更新有独立用户、Run、worker 与文件大小校验 |
| 改造点 | 酒店把记忆升级为 `guest_memories` 表 + 向量副本（偏好：房型、楼层、枕头等），支持管理端查看/清理、删除级联清理向量；**身份证、手机号、支付信息、密码标记敏感，默认不进长期记忆与知识库**（由 CredentialGuard + 敏感过滤共同保证） |
| 落地位置 | app/agents/middlewares/memory.py、app/services/、app/routers/（管理端记忆模块） |
| 阶段 | P7.7 |

### 2.13 双评估体系

| 项 | 内容 |
| --- | --- |
| 知识库评估 | JSONL 评估集（id / question / kb_code / gold_titles / primary_title / gold_answer / tags）；金标为**文档标题级**且与线上引用展示同口径——分块切分随索引重建变化，块级金标在开发态无法稳定复现，块级字段仅作参考，此为已文档化的偏离；指标 recall@K、precision@K、f1@K、primary@1、hit_rate、answer_correctness（有判定时）、overall_score 取有答案时的 answer_correctness 否则 recall@K；评估读当前检索配置逐题存结果，报告只追加；三指纹（数据集/内容/配置）判定可比性，内容或配置变化时旧报告标记 `superseded_by` + `invalidated_reason`；**内容或参考块变化后旧分数不可直接比较**（HaoChen 升级为强制作废旧基线） |
| 智能体评估 | Langfuse Dataset + CLI experiment：每条 item 经真实 API/worker/AgentRun 执行，最终输出写回 experiment；trace 标 source=agent_evaluation + dataset/item/experiment 三个维度；超时不算成功；**配置变化后必须开新 experiment 名称** |
| 酒店域增量 | 评估集冷启动：真实质检标注 + FAQ 抽样；端到端实验断言订单、派单工具链结果（验收主张 A5） |
| 落地位置 | app/eval/（kb_dataset / kb_metrics / kb_runner / kb_judge）+ CLI 评估入口；报告落 `kb_eval_reports` 表并导出到 docs/eval/ |
| 阶段 | P3（KB 基线）、P8（闭环，验收主张 A5） |

### 2.14 运行事件流与状态投影（SSE）

| 项 | 内容 |
| --- | --- |
| 机制要点 | 事件流只做**投影**，不做事实来源：消息、审计与 Run 终态以 PostgreSQL 为准，Redis Stream 按 Run 隔离承载事件（固定 TTL + 长度上限，是短时缓存与断线续传缓冲，不是存储）；事件在对应事实提交之后发射（message.completed 在消息提交后、终态事件在终态提交后），发布失败只告警、不影响 Run 与事实落库；读侧先按 Last-Event-ID 回放积压，再阻塞跟随，每轮跟随之后复核数据库 Run 状态——事件缺失（Redis 降级、租约被 reconciler 收敛、订阅晚于运行结束）时按数据库合成 message.completed 与终态事件后收流；心跳注释保活；订阅前做 Run 归属校验（不存在 404、越权 403） |
| 事件协议 | run.started、message.delta、tool.call、tool.result、message.completed、run.completed、run.failed、run.cancelled、handoff.requested、interrupt；统一包络 run_id / thread_id / agent_slug / entry_slug / seq（`agent_slug` 为实际执行者，supervisor 派发到子图后事件归到具体子 Agent；`entry_slug` 为 Run 入口 Agent，Run 级合成事件二者相等；seq 只用于消费侧排序参考，续传定位以传输游标为准）；终态事件与 Run 终态一一对应（run.cancelled 是取消终态的投影位，取消意图仍先落库 `cancel_requested`，Redis 只做加速感知）；handoff.requested 在 P1 只保留事件位（业务转人工随 P7.8 接入）；interrupt 是审批等待事件位，等待回答/审批的 resume 语义随 P2+ 补全 |
| 酒店域增量 | C 端小程序按 run_id 订阅流式回复；管理端会话监控（P7）复用同一事件协议；取消事件位与转人工事件位在业务接入前不产生生产方 |
| 落地位置 | app/services/run_event_bus.py（总线：Redis Stream 实现 + 空实现降级）、app/services/run_events.py（协议与发射器）、app/services/run_stream.py（SSE 投影）、app/routers/chat_router.py（GET /api/guest/chat/stream） |
| 阶段 | P1（验收主张 A1 的推送链路） |

P1 已落地运行事件总线与 SSE 端点：api 与 worker 进程启动时各建一份进程级总线（`create_run_event_bus`，Redis 不可达时降级为 `NullRunEventBus`，流退回数据库状态投影，不阻断进程启动）。发射侧：`RunExecutor` 在认领后发射 run.started，回复消息提交后发射 message.completed（带已提交的 message_id），ToolNode 在调用前发射 tool.call、执行与审计后发射 tool.result，审批等待发射 interrupt，终态事件在终态写入提交后发射（租约被接管时不发射，由 reconciler 收敛后按数据库投影补缺）；非流式模型（含 stub 与测试替身）成功调用后把整段回复回退为一次 message.delta。读侧：`GET /api/guest/chat/stream?run_id=` 支持标准 `Last-Event-ID` 头与 `last_event_id` 查询参数续传，只有传输游标形态（`ms-seq`）参与定位、非游标 id 从头回放，合成事件 id 带 `projected-` 前缀；活动运行按 `sse_heartbeat_seconds` 发心跳注释。配置项：`RUN_EVENT_TTL_SECONDS` / `RUN_EVENT_MAXLEN` / `SSE_HEARTBEAT_SECONDS` / `SSE_FOLLOW_BLOCK_MS`。

## 3. 中间件装配差分

基线装配顺序（12 道）：Steer → filesystem → Skills → Memory → SubAgent → Summarization → TodoList → PatchToolCalls → ModelRetry → ImageInputCompatibility → TokenUsage → 工具审批。

HaoChen 目标（11 道）= 设计文档规定的 9 道 + 新增 2 道：

| 位置 | 中间件 | 采用方式 | 说明 |
| --- | --- | --- | --- |
| 1 | Steer | 直接采用（P1 保留，业务暂不启用） | 安全边界发现待接替请求 |
| 2 | filesystem | 改造（裁掉 Sandbox provisioner/K8s，只留 Workdir 与大结果卸载） | 大工具结果落 Workdir，供确定性压缩引用 |
| 3 | **CredentialGuard** | 新建（P4 已落地） | 对工具回包与助手消息中的密码字段打码入库；阻断明文进入记忆与摘要历史文件；入参审计与事件投影用打码副本；提示词层约束模型不复述历史密码。**必须先于 Memory 与 Summarization**，否则明文可能进入记忆注入或摘要历史 |
| 4 | Skills | 直接采用 | 渐进/预加载激活与依赖开放 |
| 5 | Memory | 改造 | 酒店记忆 + 敏感过滤 |
| 6 | Summarization | 直接采用 | 确定性压缩 + 摘要，见 §2.9 |
| 7 | PatchToolCalls | 直接采用 | 修正工具调用消息形态 |
| 8 | ModelRetry | 直接采用 | 按配置重试 |
| 9 | **ToolGuard** | 新建（P4 已落地） | 副作用工具（read_only=False 或 dangerous）按「Run + 工具 + 打码入参摘要」注入幂等键；执行中的同键调用拦下（失败事实 + 模型可见错误结果），已完成的同键调用复用既有结果不重复执行；执行失败释放键允许重试。幂等键随 tool_audit 落库，同键多行即复用/重试证据。与工具审批同层、包裹工具执行边界。审批等待（interrupt）供转人工语义参考 |
| 10 | TokenUsage | 直接采用 | 近似上下文 + Provider 实际用量；Run 终态写入 AgentRun.token_usage |
| 11 | 工具审批 | 改造（业务审批策略） | 默认模式下拦截危险操作；酒店侧用于泄露处置等管理员操作 |

裁剪：SubAgent（酒店不做母子智能体）、TodoList（无前端状态面板）、ImageInputCompatibility（无图片输入工具，P6 视觉审核是独立服务不经过模型）。

P1 已在 app/agents/middlewares/ 落地全部 11 道工位（Station 基类 + pipeline 装配；prepare/before 正向、after 逆向的洋葱模型；关闭的工位整道跳过）。Steer 按计划留空实现挂位（默认关闭，键位与顺序保留）；阈值类参数只来自全局配置（`summary_threshold` 等），工位级差异走 Agent 配置 `middleware` 段的字段覆盖，未知工位或未知字段显式报错。CredentialGuard 与 ToolGuard 已按 P4.4/P4.5 落地默认开启（见 §6 与 §7 第 13、18 条）；顺序与洋葱语义由单测与真实 Run 集成测试验证，明文负例（明文不进消息库/检查点/审计/摘要历史）与幂等负例（同参副作用不二次执行）已有对应用例。

CredentialGuard 的打码口径（P4.4，默认类别于 P0 改造扩为三类）：打码落在持久化边界——工具回包进检查点与后续请求之前、助手消息入库之前、压缩历史落盘之前、工具入参进审计与事件投影之前；生成中的流式增量不审查（下发与提醒场景的实时回复可含当日密码，落库副本仍是打码形态）。默认打码三类（`DEFAULT_MASK_CATEGORIES`）：密码整值占位（`密码：[已打码]`，匹配"字段标记 + 分隔符 + 含数字且长度≥4 的值"），身份证保前 6 后 4、手机号保前 3 后 4、中间等长打码（`110101********1234` / `138****5678`）——最小化且保留可读，完整形态被打码后不再匹配敏感正则。打码类别可配置、未知类别显式失败。约束并入开头系统提示（不新增消息，历史索引不变）。已知边界：客人自己输入的消息在接入侧即落库、不经过本工位；模型生成的工具调用入参（AIMessage.tool_calls.args）随图状态进检查点，打码只覆盖审计与投影副本（改写状态会破坏 resume 重放语义）。

ToolGuard 的幂等口径（P4.5）：副作用判定取注册元数据（`read_only=False` 或 `dangerous=True`），只读工具完全不设防。幂等键 `toolguard:{run_id}:{tool}:{sha256(规范化入参)[:16]}` 从**打码后的入参副本**计算——审计里既无明文、也不构成口令枚举面（哈希前缀对低熵输入可暴破）；由此的已知边界：若未来出现「敏感字段作为入参、不同值代表不同副作用」的写工具（当前锁凭证由服务端生成、不接受密码入参，故不触发），该键会把它们并成同一次调用，届时需单独评估。执行中的同键调用拦下（失败事实 + 模型可见错误结果，Run 不中断）；已完成的同键调用复用既有结果，复用仍经 `after_tool` 复走打码链后才进入会话状态，事实记 completed、耗时 0、同键；执行失败释放键允许重试。本工位在内存里拦「同一 Run 内模型重复点名」；进程重启或 Run 恢复重放由持久层幂等键兜底（订单列内唯一约束、`checkin:{order_id}`），两层都不可省。幂等键经 `ToolResult.metadata` 交给注册骨架写进 `tool_audit.idempotency_key`。

### 3.1 为什么三个工位自研而非直接用框架能力

HaoChen 用 `langgraph~=1.0` / `langchain-openai` 承载图与模型接入，但 `patch_tool_calls`、`model_retry`、`token_usage` 三个工位是自研实现。逐工位登记理由与代码锚点（评审常追问"为什么不用框架自带"）：

| 工位 | 框架似有能力 | 自研理由 | 代码锚点 |
| --- | --- | --- | --- |
| PatchToolCalls | LangGraph 预建工具节点对悬空 `tool_calls` 的规范化 | 本项目的对话事实来自 **PostgreSQL 消息表整体重放**（非从框架检查点续跑），叠加摘要折叠与外部写入，历史里会出现悬空 `tool_calls` 或孤立 ToolMessage；OpenAI 兼容接口对此直接返回 400。框架的规范化不覆盖"从外部重放的历史被截断"这条路径 | `app/agents/middlewares/patch_tool_calls.py:1-6`（docstring 说明触发场景）、`app/services/run_body.py:104-105,143-144`（历史重放） |
| ModelRetry | `ChatOpenAI(max_retries=...)` 原生重试 | 模型层**显式关闭** `max_retries=0`，为的是让**每次尝试各写一条审计事实**（成功与失败都留痕，供可观测与失败归因）；框架内建重试不落审计 | `app/agents/models.py:111`、`app/agents/middlewares/model_retry.py:1-4`；证据 `tests/integration/test_agent_runtime.py::test_model_failure_leaves_audit_fact_and_fails_run`（默认 3 次尝试 = 3 条失败事实） |
| TokenUsage | LangChain 的 `usage_metadata` | 该工位做**两件事**：① Provider **实际用量**（复用 `usage_metadata`，含缓存命中 `input_token_details.cache_read`，供缓存命中率与成本报表）；② **近似上下文 token 估算**（CJK 按 1 字符、其余 4 字符）与 `summarization` 的阈值判断**同口径**——框架没有这个估算 | `app/agents/middlewares/token_usage.py:30-34`（估算）、`app/agents/models.py:57-68`（实际用量）、`app/config.py:61`（`summary_threshold`） |

结论：三者中只有"实际用量"一项是框架现成能力；其余（外部历史重放的消息形态修复、逐次留痕的重试、与压缩阈值同口径的近似估算）都是本项目可观测性与上下文治理口径的一部分，收敛掉会丢语义。因此**保留自研**，仅在此登记理由，不做收敛。

## 4. 裁剪清单（明确不做）

| 裁剪项 | 原因 |
| --- | --- |
| SubAgents（task/subagent_* 工具、子图） | 三个内置 Agent 职责独立、无委派关系 |
| Sandbox provisioner / Kubernetes / 沙盒命令执行 | 酒店无代码执行场景；filesystem middleware 的大结果卸载与 Workdir 语义保留 |
| 多租户 / 部门 / 用户组权限 | 单店场景，改为角色 RBAC（客人 / 保洁 / 店长 / 管理员） |
| Dify / Notion 只读连接器 | 知识库仅 Milvus executor |
| 统一智能体工作台 / 产物预览 / 文件下载 / html-preview / image-gen | 三端各自的业务界面替代 |
| 用户定时 Agent | 定时需求走领域 Durable Task（轮换、收敛、跑批） |
| Web 搜索工具 | 周边推荐用 Neo4j 图谱，不用外部搜索 |
| 个人 Skill 安装 / 远程 Skill 市场 / install_skill | 酒店 Skill 随代码发布，管理员只做启停与版本管理（保留启停界面） |

## 5. 酒店域新增

| 新增项 | 内容 | 阶段 |
| --- | --- | --- |
| 领域表与状态机 | 12 张业务表（design.md 领域模型）+ 订单/清洁状态机 + 凭证生命周期 | P2 |
| 身份与授权基座 | 不透明会话令牌（Redis，滑动过期）+ 角色守卫依赖 + 资源归属查询（客人/员工/管理端分层）；开发态身份请求头仅在开关打开时生效 | P2 |
| 凭证安全 | lock_credentials 仅 code_hash + 生效窗口；每日轮换 next→active；续订保持；退房轮换 revoked；泄露处置手动吊销；**下发 = 按需轮换**（`reason="delivery"`，旧码立即失效，配合「明文不可回读」的 A2 口径） | P4 |
| 密码下发双通道 | 会话内下发：CredentialGuard request-scoped 通道（提示只进下一次模型请求，落库副本打码）；服务通知：`checkin.prepare` 里一次性推送（best-effort + 事件留痕，未触达由前台外呼兜底） | P4 |
| 密码轮换规则 | 每日轮换 Durable Task（`lock.rotate_daily`）：到点 `next`→`active`、当日窗口缺位即兜底签发、始终预排次日 `next`；只动空置房（房态 `occupied`/`maintenance` 与住宿存续订单双口径跳过）；逐房独立事务、设备不可达即停止后续房间、存在失败房间即失败关闭；续订保持当前凭证只展期；泄露处置 = 店长手动吊销并轮换（保持原窗口、清空 `next`），管理端一次性返回明文（`app/routers/admin_lock_router.py`，admin/manager） | P4 |
| CredentialGuard / ToolGuard | 两道新中间件（§3） | P4 |
| 支付回调链路 | webhook 薄路由（验签）→ 事实先落 PG → 投递 worker；重复回调幂等 | P4 |
| vision-api | 人脸 1:1、工牌 OCR、工服检测（独立服务，不经过 LLM） | P6 |
| 保洁端打卡与任务流 | 上/下班打卡；退房清洁（当前密码+目标密码、说明、留证、确认改密）/ 呼叫清洁（免密码免说明） | P5–P6 |
| 转人工 | 业务 handoff 队列（基于 interrupt/resume 与审批语义实现，但属业务流程而非工具审批） | P7.8 |
| qa-auditor | 质检 Agent（离线跑批）：解决/未解决/转人工/投诉风险/幻觉嫌疑打标 + 风险告警 | P7.3 |
| 报表与数据中心 | 指标口径见 design.md；全部由审计事实派生；数据质量评分卡与治理动作 | P7.4、P8 |
| 三端前端 | guest-miniapp、staff-miniapp、admin-web（统一工程习惯：Vue 3；管理后台 Vite + Ant Design） | P3–P7 |

## 6. 落地任务清单与交付记录（依赖排序）

| # | 任务 | 主要内容 | 交付物 |
| --- | --- | --- | --- |
| T1 | Schema 与迁移基线 | tasks、agent_run_attempts、model/tool 审计字段、Message/Request/AgentRun、LangGraph checkpoint 表；版本化 schema 迁移（升级幂等、失败不记版本） | 迁移基线；P0 已交付审计表首迁移 |
| T2 | Repository 层 | AgentRun（lease/attempt/终态/收敛）、Request 队列读写（行锁）、审计写入与统一读 | repositories + 单测/集成测试 |
| T3 | 请求接入与 FIFO | Message+Request 提交、队头判定、Dispatch、`enqueue/reject` 策略、request_id 幂等与冲突 | 接入用例 + 集成测试 |
| T4 | Run worker | ARQ 消费、claim/lease/heartbeat、终态写入、completed 接力、启动恢复（先重投 pending）、reconciler | Run 执行器 |
| T5 | 审计埋点 | Model/Tool lifecycle 写入（来源键、seq、状态、耗时、usage）、state_reconciled、500 条读接口 | 审计写入 + 查询接口 |
| T6 | Agent 装配层 | BaseAgent 自动发现、BaseContext 配置表单链路、prepare_agent_runtime_context、极简 front-desk 占位图 | agents/ 装配层 |
| T7 | 中间件流水线 | 落地 9 道顺序（§3），CredentialGuard/ToolGuard 先留空实现挂位 | app/agents/middlewares/ |
| T8 | 工具注册骨架 | `@tool` registry、ToolNode 注册、Skill 门控钩子（无内容） | registry + 装配测试 |
| T9 | SSE 与状态投影 | Run 事件流、断线续传、interrupted/审批事件位 | 事件协议与实现 |
| T10 | 恢复演练与验收测试 | kill worker 收敛、重复投递、pending 重投递、明文扫描骨架测试 | 演练记录 + 测试套件 |

P1 恢复演练已交付：测试套件 `backend/tests/integration/test_recovery_drill.py`，记录见 [P1 恢复演练记录](./drills/p1-recovery-drill.md)。六个用例覆盖——杀 worker（租约收敛 failed + 接力队头 + 数据库投影补齐终态）、重复投递（顺序与并发均恰好执行一次）、重启补发（只覆盖 pending，他机运行中不被抢）、Redis 全故障（事实照常落库，SSE 退回数据库投影）、明文扫描骨架（真实 Run 全链路产物零敏感命中）、M1 退出标准全链路验收（消息→Run→模型→SSE→审计读接口）。演练边界：故障在单进程内注入，不覆盖真实多进程竞态；不宣称 exactly-once；明文打码与阻断行为待 P4 CredentialGuard 落地后以同一骨架升级验证。

P2 交付记录（development-plan §2.1–§2.6）：领域迁移 `backend/migrations/`（12 张业务表 + 幂等记录表 + users 账号扩展，`alembic upgrade/downgrade/upgrade` 循环在 CI 与本地均可重放）；状态机在 `app/domain/states.py` 与 `app/services/order_service.py`、`app/services/housekeeping_service.py` 落地（非法迁移拒绝、事件只追加、终态时间戳与事件台账）；授权基座在 `app/security/`（不透明会话令牌存 Redis、滑动过期 43200s、`require_identity`/`require_roles` 依赖）与路由/服务/仓储的归属查询（`app/services/staff_task_service.py`、`app/services/room_access_service.py`）；基础数据 admin API 在 `app/routers/admin_router.py` + 种子 `app/seed.py`；幂等组件 `app/services/idempotency_service.py` 以 `(scope, key)` 唯一约束 + SAVEPOINT 抢占实现「重复提交返回既有结果而非新建副作用」；越权负例与 A6 取证见 [P2 越权负向取证记录](./drills/p2-authz-drill.md)（14 个负例 + 数据不变快照）。管理后台（`apps/admin-web`）接入真实登录与酒店基础数据页，角色差异（admin 可读写 / manager 只读、账号页仅 admin）在页面与路由守卫外仍由服务端判定兜底。

边界与后续：会话令牌为不透明随机串而非 JWT，登出与禁用即时失效依赖 Redis 与账号状态每请求复核；Redis 不可用时登录返回 503、鉴权 fail-closed 视为未认证。开发态身份请求头 `X-Dev-Username` 仅在 `DEV_IDENTITY_ENABLED` 打开时可绕过登录（本地开发用，容器部署默认 `false`）。P2 交付的两个只读接口与 admin 写接口已按「路由依赖角色 + 服务/仓储归属」双层判定，P4/P5 新增写路径（接单、完工、密码下发）沿用同一套判定并各自补齐负例。

P3 交付记录（development-plan §3.1–§3.6）：迁移 `20260927_0005`（文档/chunk/图谱/评估报告）与 `20260927_0006`（评估报告基线）、`20260927_0007`（`message_feedback` 与 `handoff_requests`）；检索实现在 `app/knowledge/`（`retrieval.py` 宽捞→重排→装配、`rerank.py` 保序/覆盖度/LLM 三种重排、`tokenization.py` CJK 二元 + 字母数字串分词，复用同一口径供 BM25 与重排）；上传链路 `upload_service.py` + `task_handlers.py`（解析、分块、索引 Durable Task；PG + Milvus 双写失败置 `error_indexing`）；图谱 `graph.py` / `graph_seed.py`（房型-设施-周边，样例数据虚构距离与步行时间）；咨询工具 `app/agents/toolkits/knowledge.py`（query_kb / list_kbs，运行期上下文参数不进入工具 schema）与 front-desk 提示词 v0；评估基线 `app/eval/kb_runner.py` 落库并导出 `docs/eval/kb-baseline.json`（73 题、K=10：recall@10 0.9863、primary@1 0.3973、overall 取 recall@10 0.9863；唯一未召回 meal-08「晚上 22 点之后还能吃东西吗？」——开发态确定性向量与金标文档的相似度不足，非重排截断所致）；C 端咨询页（`apps/guest-miniapp/src/pages/chat`）消费 run 事件流（run.started / message.delta / tool.call / tool.result / message.completed / 终态），历史消息带引用（引用取该消息所属 Run 最后一次模型调用的 `retrieval_refs`）、点赞点踩与转人工入口，接口为 `GET /api/guest/chat/threads/{id}/messages`、`PUT /api/guest/chat/messages/{id}/feedback`（up/down/null 三态，null 即撤销）、`POST|GET /api/guest/chat/threads/{id}/handoff`（同线程已有 pending 时复用，201/200 区分是否新建）。

边界与后续：P3 实测跑在开发态确定性 embedding 上；对话模型已于 P4 起点接通 OpenAI 兼容端点（`deepseek-flash`，密钥由系统环境变量直通），酒店问答、工具调用、引用落库与缓存命中用量经真实链路验证（回答带 [1][2] 标注、`citations` 由最后一次模型调用的 `retrieval_refs` 派生）。0.9863 的召回仍是开发态 embedding 下的「检索链路可用」证据而非生产精度；meal-08 的收敛手段（接真实 embedding、补样例内容、必要时同义改写）留待接入真实向量模型后复测。评估集校准实验一（覆盖度打分叠加逐字查询短语加权）实测全指标下降（primary@1 0.397→0.356、recall@10 0.986→0.973），未采纳并完整回滚、配置指纹不变——校准结论以数字留档。微信用分块事件流（`uni.request` + `enableChunked`）与 H5 的 fetch 流共用同一帧解析，H5 已端到端验证（经 Vite 代理订阅真实 Run 的 SSE），微信开发者工具内的真机验证未做。转人工只落宾客侧请求事实，接单与状态推进（accepted/closed）由 P7.8 人工工作台负责；`handoff.requested` 事件仍无业务生产方。

P4 交付记录（development-plan §4.1–§4.8 已交付，§4.9 进行中）：T1 订单工具落在 `app/agents/toolkits/orders.py`（list_room_types / list_my_orders / create_order / reschedule_order / renew_order / cancel_order / apply_invoice，幂等键 + 执行处复验归属、日期窗、容量与库存，见 `tests/integration/test_order_tools.py`）。T2 微信登录与支付：`POST /api/guest/auth/wechat`（code2session → users/guests 按 openid 幂等链接 → Redis 会话）、`POST /api/guest/orders/{order_no}/pay`（归属与状态复验 → JSAPI 下单 → `payments(created)`）、`POST /api/webhooks/wechat/pay`（契约 §1.5 验签，失败 401 不写事实；事务内落支付事实、迁移订单 paid、提交 `checkin.prepare` 任务，提交后投递；`transaction_id` 唯一约束即幂等键，重复回调不产生第二事实，乱序失败回调不回退已 paid，金额不符拒绝记账）。T3 lock-mcp：`app/lock/`（契约束缚的渠道客户端 + `LockCredentialService` + `checkin.prepare` Handler）覆盖凭证生成/轮换/吊销/开锁记录，库中只存 `code_hash`（keyed HMAC-SHA256）与生效窗口；轮换/吊销携带 `expected_version` 不符即拒（契约 §2.3，mock 同步实现 409）；房间分配取房型内与本订单日期窗无重叠占用的房间并与签发同事务；`checkin:{order_id}` 幂等键重放返回既有事实、不重复下发明文；任务结果只含元数据（结果随任务落库，明文不进）。失败收敛：门锁不可达 → 任务失败关闭 + 同事务追加订单事件 `checkin_blocked`，订单保持 paid。测试：`tests/integration/test_wechat_payment.py`（10）、`tests/integration/test_lock_credentials.py`（10）、`tests/test_wechat_signature.py`（5），全套 362 通过。live 证据（2026-09-28，compose 全栈）：登录 → 下单支付 → 设备模拟器回调重复投递两次 → 数据库事实恰为 1 条 `payments(success)`、1 条 `checkin.prepare(succeeded)`，订单 paid 且已分配房间，`lock_credentials` 仅存 `code_hash`（与本地按 `LOCK_MASTER_KEY` 重算的 HMAC 一致，全表明文扫描 0 命中），设备侧以明文码开锁 accepted=true；负例：task-worker 未配 `DEVICE_SIM_BASE_URL`/`LOCK_MASTER_KEY` 时任务失败关闭，订单保留 paid 并追加 `checkin_blocked`（compose 已补齐并复测通过）。

边界与后续：lock-mcp / wechat-mcp 本阶段以进程内渠道客户端承接（`app/lock`、`app/wechat`），契约束缚、幂等与失败语义与 MCP 形态一致；`app/mcp_gateway/` 的 `streamable_http` MCP 暴露与经管理后台的注册/启停属 P7 管理链路，封装时业务服务与 §7 不变量不变。明文下发（密码、楼层房号与入住指引）属 P4-T6 checkin-flow；本阶段任务结果与订单事件均不含明文，只存元数据与引用。

T4 CredentialGuard（P4.4）已交付：`app/agents/middlewares/credential_guard.py` 默认开启（配置段可关），打码落在持久化边界——`after_tool` 打码工具回包（先于 filesystem 卸载与检查点写入）、`after_model` 打码助手消息（入库与后续请求都只见占位）、`prepare_model_call` 兜底清扫历史并把安全约束并入开头系统提示（不新增消息、历史索引不变）、`Station.redact_tool_args`/`pipeline.redact_tool_args` 让 tool_audit 与 `tool.call` 事件投影只带打码副本（`toolkits/registry.py` 统一取用，工具执行仍用模型原始入参）。占位文本（`密码：[已打码]`）自身不再构成可命中敏感串，扫描器口径收敛为「字段标记 + 分隔符 + 含数字且长度≥4 的值」。测试：`tests/test_middlewares.py` 新增 4 例（回包/助手消息打码、审计入参打码且不动执行入参、历史清扫与约束只并入一次、默认只打码密码并可配置类别、压缩前打码落盘历史），`tests/integration/test_credential_guard.py` 1 例真实 Run（模型替身在回包与答复里都带明文，断言消息库、检查点状态、tool_audit、message.completed 与 tool.call 事件零命中，并钉住「流式增量不审查」的边界），全套 368 通过（175 单测 + 193 集成）。live 证据（2026-09-28，compose 重建 api/worker）：窗口内三个真实模型 Run 中 worker 日志出现 `命中敏感明文并已打码：stage=context`（客人输入里的口令形态在模型请求前被打码）；容器内直跑装配后的流水线，助手消息、工具回包与审计入参均为 `[已打码]`；真实模型在约束下拒绝复述/虚构密码并复述「密码只在下发与提醒场景提供」的口径。边界：客人自己输入的消息在接入侧即落库、不经过本工位；模型生成的工具调用入参随图状态进检查点（改写状态会破坏 resume 重放），打码只覆盖审计与投影副本——两项已记入 §3 的口径说明，P4-T9 收口时复核。

T5 ToolGuard（P4.5）已交付：`app/agents/middlewares/tool_guard.py` 默认开启（配置段可关），`Station.reuse_tool_result` 新钩子 + `pipeline.reuse_tool_result`（先命中为准）与 `DuplicateToolCallError` 落在基类；`build_middleware_pipeline` 新增 `run_id` 参数（`front_desk/agent.py` 传 `runtime.run_id`）参与幂等键。注册骨架按分支处置：复用路径把既有结果再经 `after_tool` 复走打码链后返回（状态 completed、耗时 0、同键），拦截路径写失败事实 + `tool.result(failed)` 事件 + 模型可见错误结果，主路径把 `ToolResult.metadata.idempotency_key` 写进 `tool_audit.idempotency_key`；工具执行抛错的分支现在也过 `after_tool`（ToolGuard 借此释放在飞键，失败行同样带键，与重试行共享）。测试：`tests/test_middlewares.py` 新增 2 例（在飞拦截/完成复用/键稳定性/参数区分/失败释放，工位与流水线装配），`tests/test_toolkits.py` 新增 4 例（ToolNode 全链路复用、并发同参拦下、只读工具不设防、失败释放后重试），`tests/integration/test_tool_guard.py` 1 例真实 Run（模型连续两轮点名同参 create_order：数据库恰 1 张订单，两行 tool_audit 同键、复用行 duration 0），全套 375 通过（181 单测 + 194 集成）。边界：拦截是内存态（进程内、随 Run 生命周期），跨重启重放由持久层幂等兜底（§3 口径）；键从打码入参计算，敏感字段入参的写工具属未来评估项。

T6 checkin-flow skill（P4.6）分四步交付，T6a–T6d 已交付：

T6a Skills 内容机制与 checkin-flow 内容：内容根 `backend/skills/`（容器内 `/srv/app/skills`），一目录一 Skill；`app/agents/skills/catalog.py` 装载并校验 frontmatter（name / slug / description 必填，未知字段、缺文件、slug 与目录名不符、frontmatter 非法一律显式失败；内容根不存在视为空目录，未引入 Skill 的部署不因此失败），依赖闭包展开时对「激活集合 − 可用集合」失败关闭。首个内容 `skills/checkin-flow/SKILL.md` 声明 `tool_dependencies: [get_checkin_info, deliver_room_password]`，正文写「先核对订单与支付状态 → 索要密码时下发并说明旧密码失效 / 仅本次展示 → 楼层房号以工具返回为准、Wi-Fi/早餐/退房时间先查知识库并标引用 → 时间类问题先取当前时间」与边界（不编造、不复述历史密码、不代客支付、只处理本人订单、不把密码写进工具入参/记忆）。front-desk 声明 `skill_ids = ("checkin-flow",)`，P4 一律预加载（首轮即可用），渐进激活随 P5 首个渐进 Skill 落地。测试：`tests/test_skill_content.py`（目录校验负例、依赖闭包、指令渲染、工具按 Skill 门控），`tests/test_toolkits.py` 覆盖未激活不放行。

T6b 密码下发工具与门锁服务扩展：新增只读工具 `get_checkin_info`（房号、楼层、生效窗口与凭证版本，永不带明文）与副作用工具 `deliver_room_password`（注册元数据 `credential_delivery=True`，ToolGuard 按副作用设防）。`LockCredentialService.deliver_for_order` 把「下发」实现为**按需轮换**：`reason="delivery"`、携带 `expected_version`（旧密码立即失效，符合 A2 明文不可回读），未支付 / 非本人 / 未签发分别以 Conflict / NotFound / Conflict 拒绝且不调用设备；同一订单重放（`checkin:{order_id}` 幂等键命中且当下无新明文）返回既有事实、`code=None`，不重复下发。测试：`tests/integration/test_checkin_tools.py`、`test_lock_credentials.py`。

T6c CredentialGuard 下发通道（明文只进请求不进落库）：打码工位新增 request-scoped conduit——标了 `credential_delivery` 的工具回包在打码前取走一次性明文（`_capture_delivery`），暂存于工位实例；下一次模型请求由 `prepare_model_call` 把提示（`DELIVERY_NOTICE_TEMPLATE`）并入**开头系统消息**（不新增消息、历史索引不变，通知只活在这一个请求里，不进 state / checkpoint），模型产出非工具调用的回复即清空待下发值；`_strip_delivered` 对所有持久化副本做精确串清扫。由此明文边界为：live SSE 增量含明文（下发场景的实时回复，UI 需要）、消息库 / `message.completed` / 检查点 / 工具与模型审计 / 工具入参投影全部零命中。测试见 `tests/integration/test_checkin_tools.py` 的 Run 级用例与 `test_credential_guard.py`。

T6d 微信服务通知（订阅消息）第二通道：`app/wechat/client.py` 新增 `send_subscribe_message`（入参为语义键 `room_no/floor/code/guide` 与 `page`，模板字段映射由传输层适配，见契约 §4.4）；下发时机只有一个——`checkin.prepare` durable task 中密码签发提交之后（明文在服务端存在的唯一时刻），由 `app/services/checkin_notice_service.py` 执行：指引优先取知识库（`hotel-guest`，top-2 片段折叠空白、去 Markdown 标记后截断，检索不可用退回固定文案），渠道与配置问题一律收敛为 outcome。口径：best-effort 不重试（契约 §8-11），失败只记订单事件 `credential_notified`（`status: sent|failed|skipped` + `reason` + `template_id`，绝不记载荷），主通道与任务结果不受影响；模板未配置或客人无 openid → `skipped` 留痕；重放（`code=None`）既不下发也不记事件。任务结果新增 `notify_status` 元数据（不含明文）。设备模拟器补 `POST /wechat/v1/notify/subscribe` 与调试回读 `/wechat/v1/notify/records`；compose 经 `WECHAT_SUBSCRIBE_TEMPLATE_ID` 注入模板（留空即跳过并留痕）。测试：`tests/integration/test_checkin_notice.py`（4 例：下发载荷与事件、指引取自知识库、渠道失败不影响主通道、缺模板/缺 openid 跳过留痕）、`tests/test_wechat_client.py`（传输契约负例）、`tests/test_device_sim.py`。

live 证据（2026-09-28，compose 全栈，T6b–T6d 一起验证）：微信登录（模拟）→ 建单 → JSAPI 下单 → 设备模拟器回调**重复投递两次** → 数据库事实恰为 1 条 `payments(success)`、1 条 `checkin.prepare(succeeded)`（任务结果 `notify_status=sent`）→ 模拟器收到 1 条订阅消息，载荷含房号 `T6D3855`、楼层 `7`、6 位密码与知识库指引（无 Markdown 标记），订单事件链为 `paid → credential_issued → credential_notified(sent)`；设备侧以该明文码开锁 `accepted=true`；全表明文扫描（订单事件、任务结果、凭证表、消息、审计、记忆、检查点）0 命中，`code_hash` 与本地按 `LOCK_MASTER_KEY` 重算的 keyed HMAC 一致。负例：轮换下发时若本地存在同房同版本的凭证历史而设备侧已无该凭证（测试期重置模拟器所致状态分叉），签发按唯一约束失败关闭 → 任务失败、订单保留 paid 并追加 `checkin_blocked`，与契约 §8-1/§8-4 的处置口径一致。

全套 404 通过（197 单测 + 207 集成）；`ruff check` / `ruff format --check` / `mypy app`（136 文件）全绿；产出物来源引用扫描 0 命中。收口时修正一处测试脆弱性：评估的「未知知识库」负例原先依赖 `hotel-guest` 不在库中，而该 code 是生产默认知识库（`app/knowledge/seed.py`）且集成库为会话级共享库，改为本次唯一的缺失 code。

T7 密码轮换规则（P4.7）分四步交付，T7a–T7d 已交付：

T7a 设备侧 `next` 槽位与版本单调：模拟器补每房单调版本计数器（签发/轮换/预生成都 +1，吊销与清空 `next` 都不回退，只有 `/admin/v1/reset` 清零——修复了"重置后版本回到 1 与本地 `(room_no, version)` 唯一约束相撞"的分叉）与 `next` 槽位；新端点 `POST /lock/v1/credentials/next`（预生成，必须显式携带窗口，重复调用整体替换、旧 `next` 置 revoked）、`/next/activate`（转正并把旧 `active` 立即 revoked，不产生新明文）、`/next/revoke`、`/extend`（只改窗口终点，不换码不换版本）、`GET /credentials/{room_no}/next`；`POST /lock/v1/unlock` 强制生效窗口（窗外码 `accepted=false`）。`LockClient` 同步补齐四个方法，`issue/rotate` 的 `valid_hours` 变为可选并支持显式 `valid_from`/`valid_to`；契约 §1.6（reset 清零版本的注意事项）、§2.1、§2.2 同步。测试：`tests/test_device_sim.py`（+6 例，含版本跨吊销单调、`next` 替换与守卫、展期不换码、窗外码拒绝）、`tests/test_lock_client.py`（真实 client ↔ 真实模拟器的 ASGI 契约测试）。

T7b 轮换/续订/泄露用例与仓储：`LockCredentialService` 新增 `plan_daily_rotation`（候选 / 在住 / 维保三分类）、`rotate_daily_room`（三步幂等：到点 `next` 转正 → 当日窗口缺位或错位即兜底签发 → 始终预排次日 `next`；转正前先吊销旧 `active`，满足"单间唯一 active"的部分唯一索引；执行处复验房间存在、非维保、房态非在住且无住宿存续订单）、`extend_for_renewal`（续订只把 `valid_to` 展到新退房日期对应窗口，1 分钟容差内不触达设备，门锁失败上抛由调用方失败关闭）、`handle_leak`（清空 `next` + 保持原窗口轮换，窗口已过期则按最短窗口重签，`expected_version` 守卫）。仓储补 `get_next_for_room` / `activate_next` / `revoke(from_statuses=...)` / `list_room_ids_with_open_stay`；`ORDER_STAY_STATUSES`（`paid`/`checked_in`/`checkout_pending`）成为签发与轮换跳过集的唯一口径；续订工具 `renew_order` 在服务内展期，门锁不可达即失败关闭（改期不生效，不出现"订单已续、密码失效"）。

T7c 每日轮换 Durable Task、cron 调度与泄露处置 API：`lock.rotate_daily` v1 Handler 逐房独立事务（已生效的房间不回滚），结果只含元数据（房间号、版本、窗口、是否转正）；在住/维保按计划跳过并留痕，执行处复验冲突（ConflictError/NotFoundError）记为跳过，设备不可达立即停止后续房间（其余记 `device_unavailable` 跳过），存在失败房间即抛错失败关闭（错误文本给出计数 + 压缩 JSON 摘要，逐房明细在日志）。`TaskRepository.exists_by_dedupe_key`（任意状态）+ cron `submit_daily_rotation_job`（酒店本地 00:05 起、全天每 5 分钟探测、去重键 `lock.rotate_daily:{date}`、每天至多提交一次、终态不自动重放）注册进 `TaskWorkerSettings`。管理端新增 `app/routers/admin_lock_router.py`（admin/manager）：`POST /api/admin/lock/rotation/run`（人工重跑，202 + 任务标识）、`POST /api/admin/lock/rooms/{room_no}/credential/rotate`（泄露处置，明文只随该次响应一次性返回，日志只记房间号与版本）、`GET /api/admin/lock/rooms/{room_no}/credentials`（active/next 元数据视图，结构上不含明文与 `code_hash`）。

T7d 测试与 live 验证：门锁替身升级到新契约（`next` 槽位、版本单调、显式窗口、`extend`/`activate`/`revoke_next`，`codes` 始终是当前生效码）；新增 `tests/integration/test_lock_rotation.py`（14 例：当日窗口签发 + 次日预排、到点转正并作废旧凭证、同日重跑空转、漏跑日自愈、在住/在住房态/维保跳过、续订展期不换码且重放不触达设备、无凭证时续订为空操作、泄露处置轮换 + 清空 next + 明文零落库、期望版本不符拒绝、过期凭证按最短窗口重签、任务级只轮换空置房且结果无明文、设备不可达失败关闭、cron 时间窗与去重键（含终态不重放与人工重跑）、管理端角色与明文边界）。修出一处真实缺陷：`rotate_daily_room` 原先"先转正后吊销"，撞 `uq_lock_credentials_active_room`，已改为先吊销旧 `active` 再转正。全套 426 通过（204 单测 + 222 集成），`ruff` / `mypy app`（137 文件）全绿。

live 证据（2026-09-28，compose 重建 api / task-worker / device-sim）：管理端触发 `POST /rotation/run` → 202 + `task_id`，同日重复触发返回同一任务（`deduplicated=true`）；任务 `succeeded`，结果 `rotated=11`、`skipped=4`（4 间全为 `occupied`：301 / 302 / LIVE301 / T6D3855，均有住宿存续订单），房间 303 得到当日窗口 `active(v1, reason=daily)` + 次日 `next(v2)`，设备侧窗口与版本完全一致；泄露处置（manager 令牌）→ 200、`v3(reason=leak)`、窗口与 v1 相同、`next` 清空，设备侧以新码开锁 `accepted=true`、旧码 `accepted=false`；`expected_version=1` 重放 → 409「凭证版本不匹配：期望 1，当前 3」；元数据视图与响应文本均不含 `code`/`code_hash`；staff → 403、匿名 → 401。cron 侧：worker 启动即注册 `cron:submit_daily_rotation_job`，每 5 分钟触发一次，当日已存在任务行（含 `failed`）时返回空、不重复提交；把当日去重键让出后，下一次触发（14:55 本地）即提交并执行成功（`rotated=11`、`skipped=4`），且已轮换房间的版本与设备侧状态零变化（同日重跑空转），泄露处置过的房间由该次任务补回次日 `next`（v3 active + v4 next）——两套机制自然衔接。明文扫描：设备侧 21 个明文码与库中 `code_hash` 按 `LOCK_MASTER_KEY` 重算的 keyed HMAC 逐一对齐，`lock_credentials`/`tasks`/`orders`/`payments`/`messages`/`tool_audit`/`model_audit`/`rooms` 八表 0 命中。负例：device-sim 未随代码重建（缺 `next` 端点，`POST /credentials/next` 落到 `GET /credentials/{room_no}` 路由 → 405）时，任务失败关闭，错误摘要给出 11 间 `lock_error status=405` 与 4 间 `occupied` 跳过，在住房未被触碰、逐房事务回滚不留半成品；本地与设备版本分叉时（模拟器重启后设备侧无凭证、旧镜像 rotate 回退到 v1），泄露处置按 `(room_no, version)` 唯一约束失败关闭 → 409，不写入冲突行。

T8 C 端订单页面（P4.8）分四步交付，T8a–T8c 已交付：

T8a 客人订单与房型 REST API（`app/routers/guest_order_router.py` 与 `guest_room_type_router.py`）：房型只读查询 `GET /api/guest/room-types`（可选日期窗则返回 availability）与 `GET /api/guest/room-types/{code}`（不含窗时只给房型事实、不含 availability 字段）；`ensure_bookable_window` 从 OrderService 抽出为公开方法供房型与下单复用同一日期窗口径（不早于今天、1–MAX_NIGHTS 晚）；下单 `POST /api/guest/orders`（客户端幂等键按 `guest:{guest_id}:{client_key}` 命名空间化，他人键不可能命中；重放以 200 区分新建 201）、改期 `/reschedule`、续订 `/renew`（经 `booking_service.renew_booking_with_credential` 展期，门锁不可达失败关闭 409）、取消 `/cancel`、发票 `/invoice`（一单一张、重放回放 200）、支付 `/pay`（信道失败 502/503 不落事实）、列表 `GET /api/guest/orders`、详情 `GET /api/guest/orders/{order_no}`（凭证只含版本与生效窗口、永不带明文，不存在的订单与不可见资源同为 404、非归属 403）。授权口径：`PermissionDeniedError` → 403（匹配机制基线 §7 不变量 #15），`NotFoundError` → 404。测试：`test_guest_orders_api.py`（10 例：房型列表/窗口/负例、下单与按客人命名空间重放、窗/容量/库存校验、详情归属 404、改期/取消/终态守卫、续订保持凭证且展期、锁不可达失败关闭、发票一单一张 201/200、未认证 401）。

T8b 小程序真实微信登录与令牌请求层：`stores/auth.ts` 重写为 Bearer 令牌会话（`token`/`expiresIn`/`userId`/`username`/`displayName`/`role`/`guestId`/`nickname`/`loggedInAt`），新增 `wechatLogin(code)`（调 `POST /api/guest/auth/wechat`）与 `devLogin(username, password)`（H5 开发态调 `POST /api/auth/login`），`getToken()` 暴露令牌；`api/client.ts` `authHeaders()` 以 `Authorization: Bearer <token>` 替代 `X-Dev-Username`；`pages/login/index.vue` 平台检测（`globalThis.wx`）：微信小程序 → `uni.login()` + code2session，H5 → 用户名密码表单项。导航守卫、页面级 `requireAuth`、`signOut`/`clearSession` 保持向前兼容，登录后在 storage 中持久化会话。

T8c 订单页面（`apps/guest-miniapp/`）：房型列表 `pages/rooms/index.vue`（日期选择 + 可用房型 + 余量标签 + 金额预览）；预订 `pages/rooms/booking.vue`（房型信息 → 日期选择 → 客人计数 → 费用明细 → 幂等提交 → 跳转订单详情）；我的订单 `pages/orders/index.vue`（API 列表 + 空态 + tap 进详情）；订单详情 `pages/orders/detail.vue`（状态/金额 hero + 操作按钮区：支付 `uni.requestPayment` MP / H5 dev 提示、取消确认弹窗、改期底部弹出日期选择、续订底部弹出退房日选择、发票申请弹窗 + 订单信息表 + 门锁凭证元数据 + 发票信息 + 事件时间线）。所有写操作后刷新本地状态，错误以 `uni.showToast` 吐司提示。

代码检查：`vue-tsc --noEmit` 通过、`vite build` 通过（仅 Sass 废弃警告无实际错误）。后端全套 436 通过：`206 passed, 230 skipped`、`ruff`/`mypy` 全绿。

T9 安全与幂等测试 + 收口（P4.9，2026-09-29）：

P4 三项目标码全链路验证：

1. **全表明文扫描**（`test_p4_plaintext_scan_across_all_tables`）：播种（创建客人→下单→支付→入住签发凭证→模型点名下发工具→执行完成）后，对 `lock_credentials`/`tasks`/`orders`/`payments`/`messages`/`tool_audit`/`model_audit` 七表逐行运行 `scan_sensitive_plaintext()`，0 命中；`code_hash` 经 `LOCK_MASTER_KEY` 重算的 keyed HMAC 与库中值逐一对齐；订单事件链无明文。

2. **客人 REST 越权负例**（`test_guest_order_endpoints_reject_unauthorized`）：匿名 → 401（列表/下单/详情/取消/支付/房型）、伪造令牌 → 401、他人订单 → 403/404（详情/改期/取消/续订/发票/支付）、本人正常 → 200。匹配机制基线 §7 不变量 #15。

3. **重复回调幂等**（`test_webhook_applies_facts_once_and_dispatches_checkin`，P4-T2 测试）：同一 `transaction_id` 重放返回 `replayed` 状态，不产生第二 `paid` 事件、不重复创建 `checkin.prepare` 任务。

测试文件 `backend/tests/integration/test_p4_security_audit.py`（3 例全通过），`ruff` 全绿。

## 7. 运行不变量 checklist（勿丢语义）

1. ARQ 投递必须在 PostgreSQL 提交之后；`pending` 是已提交的投递意图，不是"未投递"。
2. 同一 (用户, Agent, 线程) 同时只有一个活跃 Run；completed 后接力队头；启动恢复先重投 pending 再处理新队头。
3. lease 过期一律收敛为 `failed(worker_lease_expired)`，不宣称 exactly-once；attempt 表只记事实、不可变、不回填历史。
4. 取消：先落库 `cancel_requested`，Redis 只做加速通知；worker 复核数据库后写终态。
5. `request_id` 幂等：复用返回已有对象，跨范围复用返回冲突。
6. Durable Task：handler_version 未知 fail-closed；claim 上限 4；失联不自动重放；failure hook 在同一终态事务内收敛关联对象。
7. 工具"可执行"≠"模型可见"；未激活 Skill 的工具不可调用；所有副作用执行处复验身份与目标。
8. 知识库 `parsing/indexing` 条件抢占；PG+Milvus 双写失败需补偿并置 error_indexing；批量入口只扫允许状态 + dedupe key。
9. 压缩：PG 消息不删；先确定性压缩后摘要；摘要状态以 checkpoint 中 `summarization_events` 最新一条为准；内部摘要不流给用户。
10. 审计：普通 History/Memory/Dashboard 不见审计行；读接口 500 条 + `truncated`；seq 冲突显式失败。
11. Langfuse 预创建 trace 固化不可被 callback 覆盖；Langfuse 不可用不阻断 Run；本地审计是权威事实。
12. 评估：内容/配置变更后旧分数不可比，必须开新实验。
13. 密码（酒店新增，P4.4 落地）：明文不落库、不进消息历史/记忆/知识库/审计导出；只在设备与下发载荷出现；CredentialGuard 先于记忆与摘要。打码落在持久化边界（工具回包、助手消息、压缩历史、审计与投影入参），生成中的流式增量不审查；占位形态（`密码：[已打码]`）自身不再构成敏感串。
14. 运行事件（酒店新增）：事件流是投影不是事实来源；message.completed 与终态事件只在对应事实提交后发射，发布失败不影响运行；Last-Event-ID 只认传输游标（`ms-seq`），数据库合成的投影事件不参与续传定位。
15. 授权（酒店新增，P2 落地）：角色判定在路由依赖、归属判定在 services/repositories，前端隐藏不作为边界；未认证/凭证失效 → 401，已认证但无权限或非归属 → 403，不可见资源（含已吊销、不存在） → 404 且不区分「不存在」以避免目录探测；伪造令牌不回退到开发态身份请求头；账号状态每请求复核，禁用即时失效。
16. 会话反馈与转人工（酒店新增，P3 落地）：反馈只落 `message_feedback`（同身份同消息唯一，改评即更新、取消即删除），外部可观测平台只作排障视图；评分只针对助手回复；转人工同线程待处理请求幂等复用，不重复建单；历史消息引用取该 Run 最后一次模型调用的 `retrieval_refs`，不复制进消息行。
17. 支付与门锁凭证（酒店新增，P4 落地）：回调验签失败不写任何事实（401）；`transaction_id` 唯一约束即幂等键，重复回调返回成功不产生第二事实，乱序失败回调不回退已 paid；支付事实与 `checkin.prepare` 任务同事务提交、提交后投递；凭证 `code_hash` 为 keyed HMAC（`LOCK_MASTER_KEY`），明文零落库（表、任务结果、订单事件、消息与审计）；轮换/吊销携带 `expected_version`，不符拒绝（防旧消息重放改密）；`checkin:{order_id}` 幂等键重放返回既有事实且不重复下发明文；门锁失败 → 任务失败关闭 + `checkin_blocked` 事件，订单保持 paid 不得进入 completed。
18. 副作用工具幂等（酒店新增，P4.5 落地）：同一 Run 内同工具同参（含打码入参）视为一次副作用——执行中拦下、已完成复用既有结果（复用仍经打码链）；执行失败释放键。幂等键从打码入参计算并落 tool_audit（同键多行即复用/重试证据），不出现明文也不构成口令枚举面。拦截是内存态，跨重启/Run 恢复重放由持久层幂等键兜底，两层都不可省。
19. 密码下发（酒店新增，P4.6 落地）：下发即轮换（`reason="delivery"` + `expected_version`，旧码立即失效）；明文只在三处出现——设备侧、会话内下一次模型请求的提示（request-scoped，进 state/检查点之前被清空，落库副本走打码）、订阅消息载荷。订阅读写口径：best-effort 不重试，每次尝试以订单事件 `credential_notified` 留痕（不含载荷），重放（无新明文）既不下发也不留痕；任务结果、订单事件、消息、检查点、审计、记忆零明文。
20. 密码轮换（酒店新增，P4.7 落地）：每日轮换只动空置房——房态 `occupied`/`maintenance` 与住宿存续订单（`paid`/`checked_in`/`checkout_pending` 且退房日 ≥ 当日）双口径跳过，在住与在途客人手里的码不动（续订走展期）；窗口按酒店本地日界（当日 `[00:00, 24:00)`、次日 `next` `[明日 00:00, 后日 00:00)`），同日重跑空转、漏跑日自愈（过期 `next` 作废重排）；转正必须先吊销旧 `active`（单间唯一 `active` 由部分唯一索引兜底）；提交每天至多一次（去重键 `lock.rotate_daily:{date}`，任意终态都不自动重放），失败日人工重跑；逐房独立事务，设备不可达即停止后续房间，存在失败房间则任务失败关闭（错误摘要只含元数据）；续订展期在容差内不触达设备、门锁失败则续订不生效（不得出现"订单已续、密码失效"）；泄露处置保持原窗口、清空 `next`、`expected_version` 不符即拒，明文只随该次响应一次性流转（日志与库中只有房间号与版本）。

## 8. 验收主张关联

| 验收主张 | 依赖的机制 |
| --- | --- |
| A1 三端主链路闭环 | T3–T4 请求/Run、2.14 运行事件流与 SSE、2.3 Durable Task（轮换/收敛）、2.7 MCP 网关、P5 派单 |
| A2 密码零明文 | §3 CredentialGuard 位置（含 request-scoped 下发通道，P4-T6c）、2.10 审计打码、2.12 记忆敏感过滤、订阅消息载荷边界（契约 §4.4）、明文扫描测试 |
| A3 事实先落库与幂等 | 2.1 投递顺序、2.2 attempt 事实、2.3 dedupe、支付回调用例 |
| A4 指标由审计派生 | 2.10 审计事实与读接口、P7 报表用例（不读 Redis 事件） |
| A5 数据中心闭环 | 2.8 知识库状态与评估、2.13 双评估、P8 治理动作 |
| A6 服务端授权 | 2.4 资源归一化 + 执行处复验、2.5 工具授权、repository 可见性查询；P2 取证见 [P2 越权负向取证记录](./drills/p2-authz-drill.md) |

## 9. 未决问题（P1 前需确认）

1. CredentialGuard 与 ToolGuard 的挂点已在流水线中固定（第 3 道、第 9 道）并分别按 P4.4/P4.5 落地；拦截点已由真实 Run 集成测试复验（工具回包与助手消息的明文在进入持久化与记忆之前被打码；同参副作用不二次执行）。Steer 仍为空实现占位（第 1 道，默认关闭），待业务需要时启用。
2. 微信服务通知（订阅消息）通道的模板、频率与触达率属外部能力，已在[外部系统接口契约 v0](./contracts/external-systems-v0.md) §7 登记为未验证项；P0.2 预研结论见该文 §9-6（需人工兜底，主通道为订单页/站内消息）。下发链路已于 P4-T6d 落地（契约 §4.4：唯一调用点、best-effort、事件留痕、重放不重复触达），模拟器已接收验证；真实模板字段映射与触达率待 P9 真机实测。
3. qa-auditor 的质检跑批触发方式：**已定稿为定时 Durable Task**（`qa.audit` v1，task worker 小时级 cron + 小时级去重键）扫描增量会话，管理端 `GET /admin/quality/audit` 保留同步触发；`qa-auditor` 登记为 Agent 但 `chat_entry=False`，接入侧在建 Run 之前拒绝客人指定该 slug。
