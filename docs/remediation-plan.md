# HaoChen 改造方案（Remediation Plan）

状态：待评审（draft）
日期：2026-10-12
目标：把「包装 > 实现」的部分补齐为真实能力，把「讲得圆」的部分补成「讲得清」，覆盖 Agent 能力、评测体系、检索真实性、生产面四条工作流。

---

## 0. 决策前提（已与项目所有者确认）

| 维度 | 结论 |
| --- | --- |
| 核心目标 | 求职简历 + 真实生产 两者兼顾 |
| 时间预算 | 不设限（允许系统性重做几个模块） |
| 外部资源 | 有 LLM API Key（DeepSeek，仅 chat）；外部系统（微信 / 门锁 / 机器人）**只有模拟器** |
| 处理倾向 | 补齐实现（把空壳做成真的），而非仅改表述 |
| 多智能体形态 | Supervisor + 子 Agent（LangGraph 子图） |
| 评测 gold 来源 | LLM 生成候选 + 人工审核 |
| 重型组件 | Neo4j 图谱 / DuckDB 数据湖 补齐成真的 |
| Embedding / Rerank 供给 | **本地 BGE-M3（embedding）+ bge-reranker（cross-encoder）**，零外部依赖 |
| 生产部署目标 | 单机加固（反向代理/TLS、不暴露数据端口、移除默认弱口令、预留 k8s 路径） |

**贯穿原则**
1. **每个宣称必须有可复现的自证**（评测报告 / 测试 / 一次可演示的调用）。
2. **失败关闭优先于静默降级**：缺配置要显式报错或降级并留痕，而不是悄悄跑回退实现。
3. **每期结束都必须处于「可讲圆」状态**：不能出现"改到一半，比现状更讲不圆"。

---

## 1. 现状判定表

> 「真实程度」= 代码是否真的实现了 README 的宣称。这是改造的起点清单。

| # | 问题 | 真实程度 | 处理方式 | 预估工作量 | 阶段 |
| --- | --- | --- | --- | --- | --- |
| 1 | 管理端配置链路（system_prompt/model/temperature）运行时被忽略 | 服务层已实现，**只差接线** | 补齐实现 | 小（0.5 天） | P0 |
| 2 | 记忆工位空转（`MemoryStation` 默认关、pipeline 未注入 provider） | 存储 + Provider **已实现**，只差接线 | 补齐实现 | 小（0.5 天） | P0 |
| 3 | 隐私脱敏只覆盖门锁密码，不覆盖手机号/证件号 | 部分 | 补齐实现 | 小 | P0 |
| 4 | `patch_tool_calls` / `token_usage` / `model_retry` 重造框架轮子 | 能用但重复 | 收敛为"薄适配层" | 小-中 | P0 |
| 5 | 11 道工位中 steer / tool_approval 默认空转 | 空壳 | 补齐（steer 接取消接力；approval 接 interrupt/resume） | 中 | P0/P1 |
| 6 | 单 Agent 套 LangGraph 外壳 | 单 Agent | 新增 Supervisor + 子 Agent | **大** | P1 |
| 7 | Agent 评测执行器是 `ScriptedCaseExecutor`，生产入口不注入真实执行器 | 空壳 | 补齐实现 | 中 | P2 |
| 8 | LLM-as-judge 是占位（恒返回 None） | 空壳 | 补齐实现 | 中 | P2 |
| 9 | Agent 评测集 8 条且期望工具名与真实工具不符 | 失真 | 重写数据集 + 对齐工具名 | 中 | P2 |
| 10 | KB 评测 recall@10 = 0.9863 属指标通胀，gold 是标题级 | 可自证但弱 | 改口径 + 换真实 embedding 重测 | 中 | P2/P3 |
| 11 | embedding 默认 64 维哈希、rerank 默认关键词 | 回退实现 | 接真实 embedding + rerank | 中 | P3 |
| 12 | Neo4j 只有静态 lookup，无图推理 | 过度设计 | 补齐多跳推理 | 中 | P3 |
| 13 | DuckDB 数据湖"提速 10-100x"未验证 | ✅ 已验证（P3.3：20 万订单 568→1.5ms，结果一致，见 `docs/eval/datalake-benchmark.md`） | —— | 中 | P3 |
| 14 | 微信/门锁/机器人全是模拟器 | 契约级 | **无法真补齐**（无资源）→ 如实叙述 + 强化契约测试 | 小 | P4 |
| 15 | 部署单机、DB/Redis 端口暴露、默认弱口令 | Demo 级 | 加固 | 中 | P4 |
| 16 | 无告警/SLO/on-call；Langfuse 默认可能外发隐私 | 缺失 | 补齐观测 + 脱敏 | 中 | P4 |
| 17 | 限流按 IP、无 per-user 配额 | 部分 | 补齐 | 小 | P4 |
| 18 | 备份无定时/异地/恢复演练自动化 | 部分 | 补齐 | 小 | P4 |

---

## Phase 0 — 地基接线（先把「已实现但没接」的接通）

**目标**：用最小改动消除三条最致命的「讲不圆」，且全部有测试自证。

### P0.1 接通管理端配置链路
- **根因**：`backend/app/services/run_body.py:131,137` 传 `config={}` / `middleware_config={}`。
- **改动**：
  1. `run_body.py` 在装配 `RuntimeContext` 前，用 `AgentConfigService`（`app/services/agent_config_service.py`）按 `identity.agent_slug` 读取 `config_json`；拆出 `context` 段传入 `config=`、`middleware` 段传入 `middleware_config=`。
  2. `enabled_tools` / `enabled_skills` / `enabled_knowledge_bases` 参与 `AgentResourceRequest` 的 refs 过滤（当前只按 `agent.tool_ids` 静态声明）。
  3. 校验：`FrontDeskContext.from_config`（`app/agents/context.py`）对未知键显式失败，避免配置漂移静默。
- **自证**：集成测试——写一条 `AgentConfig` 覆盖 `system_prompt`，运行 Run，断言审计里 system prompt 命中覆盖值。
- **风险**：低。这是纯接线。

### P0.2 接通长期记忆工位
- **根因**：`pipeline.py:286` 的 `MemoryStation` 未收到 `memory_provider` 且默认 `enabled=False`。
- **改动**：
  1. `MemoryStation` 注入 `AdminMemoryService`（`app/services/admin_memory_service.py:1` 已实现 `MemoryProvider` / `load`）。
  2. `run_body.py` 构建上下文时构造 provider 并传入 pipeline；`MemorySettings.enabled` 默认改为随"存在 provider"打开（或由 Agent 配置 middleware 段控制）。
  3. 记忆写入链路：Run 结束后按策略抽取候选记忆落 `GuestMemory`（补一条后台任务，见 P1.4）。
- **自证**：集成测试——预置一条 `GuestMemory`，运行 Run，断言模型请求消息里含记忆块且敏感条目被丢弃。
- **风险**：低-中（敏感过滤逻辑 `memory.py:87` 已存在，复用即可）。

### P0.3 隐私脱敏扩展到手机号/证件号
- **根因**：`credential_guard.py` 只处理门锁密码类明文；`scan_sensitive_plaintext` 已能检三类（见 `acceptance-matrix.md` A2），但**运行时脱敏通道只用于密码**。
- **改动**：把手机号、身份证号纳入审计落库前的统一脱敏（`AuditRecorder` 入参副本 redact 钩子），并扩展导出/日志。
- **自证**：负向测试——构造含手机号/身份证的对话与工具回包，断言 DB 与导出物零明文。

### P0.4 收敛重造的框架轮子
- **改动**：`patch_tool_calls.py` / `token_usage.py` / `model_retry.py` 保留行为但内部改为薄适配（能用 LangChain 原生能力的用原生；不能的保留并注明"框架无等价能力"的理由）。
- **目的**：不是为了删代码，而是让"为什么自研"有站得住的答案（面试可讲）。
- **自证**：现有单测全绿。

### P0.5 steer / tool_approval 补真
- **steer**：`app/agents/middlewares/steer.py` —— 接取消/接力链路：检测到 thread 有新用户消息待接管时，触发当前 Run 的 `cancel_event`（复用 `run_worker.py` 已有取消机制），并在新一轮 Run 里注入"用户已改口"提示。
- **tool_approval**：接 LangGraph `interrupt`/`resume`——危险工具调用时中断图、落"待审批"事实、由管理端审批后 resume。当前实现只会把 Run 弄失败（`tool_approval.py:82`）。
- **自证**：测试——危险工具触发 interrupt，审批通过后 resume 完成；拒绝则收敛为 denied。

**Phase 0 验收**：配置链路/记忆/脱敏各有集成测试；`docs/acceptance-matrix.md` 新增对应证据行。

---

## Phase 1 — 多智能体运行时（Supervisor + 子 Agent）

**目标**：让「多智能体」在代码里成立——有 supervisor、有子图、有 agent 间路由，且不只是"多写一个 Agent"。

### P1.1 Agent 拓扑
采用 LangGraph supervisor 模式，保留单一对外入口（chat）：

```
START → supervisor（决策节点，结构化输出决定下一步）
          ├─→ consult_agent（前台咨询子图：KB + 房型 + 时间）
          ├─→ service_agent（客房服务子图：清洁/送物，触发任务派发）
          ├─→ order_agent（订单子图：预订/改期/续订/取消/发票）
          └─→ handoff（投诉/资金/紧急 → 转人工工单，复用现有 handoff 链路）
                 └─→ END（回答完成）
       supervisor ←── 各子图返回后回到 supervisor 汇总
```

- **subgraph**：每个子 Agent 是独立 `StateGraph` 子图，复用同一 `MiddlewarePipeline`（见 P1.2），各有自己的 `context_schema` 与 `tool_ids`。
- **路由**：supervisor 用 `Command(goto=...)` 或条件边；子图返回后回到 supervisor 决定是否再派发或收尾。
- **对外不变**：`chat_router` / SSE / Run 执行器（`run_worker` / `run_body`）无感——图入口仍是 `AgentRunBody` 按 slug 取 agent 编译图。

### P1.2 共享中间件流水线
- 抽 `build_middleware_pipeline` 为所有子 Agent 共用工厂（当前仅在 `front_desk/agent.py:93` 内联）。
- 11 道工位对子图统一生效；凭证打码、tool_guard 幂等、token_usage 全树一致。

### P1.3 子 Agent 实现清单
| 子 Agent | slug | 职责 | 工具来源 |
| --- | --- | --- | --- |
| 前台咨询 | `front-desk-consult` | 知识问答、房型、时间 | `knowledge.py` / `system.py` |
| 订单 | `front-desk-order` | 预订/改期/续订/取消/发票 | `orders.py` |
| 客房服务 | `room-service` | 清洁/送物/机器人派发 | `room_service.py` |
| 转人工 | `handoff` | 投诉/资金/紧急 → 工单 | 复用 `conversation_service` handoff |
| 后台质检（可选） | `qa-auditor` | 后台跑批质检，不入 chat 图 | `qa_auditor_service.py` |

> 每个子 Agent 的 `AGENTS.md` 沿用现有 front_desk 的高质量提示词风格（`front_desk/AGENTS.md` 是很好的模板）。

### P1.4 记忆写入链路（承接 P0.2）
- 新增后台任务：Run 终态后按策略抽取候选记忆 → 写 `GuestMemory`（可 LLM 抽取 + 规则校验）。

### P1.5 注册表与配置
- `registry.py:41` 已支持自动发现，新增子包即可注册；注意 supervisor 与子 Agent 的 slug 命名与 `AgentConfigService` 列表展示。

**Phase 1 验收**：
- 一张可 `mermaid` 出图的真实多层图（supervisor + 3-4 子图）。
- 端到端测试：一条"我要订房 + 顺便叫一份早餐到房间"的复合请求，被 supervisor 正确拆分到 order + service 两个子 Agent。
- 审计事实里能看到 supervisor 的路由决策与子 Agent 归属。

**风险 / 待定**：
- 子 Agent 粒度划分是否按上表？（见第 5 节待确认）
- supervisor 决策用 LLM 结构化输出 vs 规则兜底——建议 LLM 为主 + 关键词兜底，避免路由错。

---

## Phase 2 — 评测体系真跑通

**目标**：把"评测"从空壳变成能自证的能力，这是简历最硬的一块。

### P2.1 真实执行器
- 新增 `RealCaseExecutor`：调用真实编译图（同 `run_body` 路径），返回实际工具调用序列与任务成功标志。
- `runner.py:138` 的默认 `ScriptedCaseExecutor` 仅保留给测试；生产入口 `admin_eval_agent_router.py:126` 注入 `RealCaseExecutor`。
- **自证**：跑一次评测，断言执行器不是 scripted（例如断言至少一条用例产生真实工具调用）。

### P2.2 数据集重建（LLM 生成 + 人工审核）
- 扩：
  - Agent 评测集：8 → **50+ 条**，覆盖订房/改期/取消/发票/客房服务/转人工/多轮/边界（拒绝+追问）。
  - KB 评测集：73 → **150+ 条**，gold 从**标题级升级到 chunk 级**。
- 生成流程：LLM 生成候选（问题 + 期望工具 + 期望答案要点）→ 导出 Excel/MD 供人工逐条审核 → 入库。
- **对齐工具名**：`orders.yaml:12` 的 `inquire_room_type` → 真实 `list_room_types`；`checkin.yaml` 的 `check_in` → `get_checkin_info`；`dispatch.yaml` 的 `dispatch_cleaning` → `request_room_clean`。加一条测试：**数据集期望的工具名必须存在于 `default_registry().names()`**，防止再次漂移。
- **自证**：新增测试 `test_eval_dataset_tools_registered`。

### P2.3 LLM-as-judge 真实现
- `eval/agent/judge.py`：实现答案质量评分（相关性/事实性/语气/边界遵守），输出 0-1 + 理由。
- 降低自评偏差：
  - **judge 与生成用不同模型或不同温度**（当前 KB judge 与生成同模型，`kb_judge.py`）；
  - 加 **self-consistency**（多次采样取中位）；
  - 抽 **10-20 条人工标注**作为 judge 的校准集，报告 judge 与人工的一致性（如 Cohen's kappa）。
- **自证**：评测报告里同时给出 `answer_correctness` 与人工校准一致性。

### P2.4 指标诚实化
- KB 报告以 **`primary_top1_rate`** 与 **chunk 级 recall** 为主指标，`recall@10` 降为参考。
- README 里的数字全部替换为"真实 embedding + 真实 rerank + chunk 级 gold"下的重测值（P3 完成后回填）。
- **自证**：`docs/eval/kb-baseline.json` 重新生成，`retrieval_config` 显示真实 provider（不再是 `dev-deterministic:64`）。

**Phase 2 验收**：
- `pytest -m eval` 真跑通；报告落库且可复现（fingerprint 机制 `runner.py` 已有）。
- README 评测段落每个数字都能追到一份报告。

---

## Phase 3 — 检索真实性

**目标**：让 Milvus / rerank / Neo4j / DuckDB 从"回退实现"变成"真实能力"。

### P3.1 真实 embedding + rerank（本地 BGE-M3 + bge-reranker）

**根因**：当前 `MODEL_BASE_URL=https://api.deepseek.com/v1` 只有 chat 端点，DeepSeek **不提供 embedding / rerank**。因此 `build_embedding_provider()`（`knowledge/embedding.py:142`）与 `build_reranker()`（`knowledge/rerank.py:197`）在生产配置下必然落回 dev 实现——这正是 `kb-baseline.json` 里 `"embedding": "dev-deterministic:64"`、`"reranker": "keyword"` 的原因。

**方案**：本地起一个 BGE 推理服务，作为独立的 OpenAI 兼容端点；后端**沿用已有的 `OpenAICompatibleEmbeddings`（`embedding.py:72`）与新增一个 OpenAI 兼容 reranker**，不引入新协议。

1. **BGE 服务选型**：`text-embeddings-inference`(TEI) 或 `Infinity`，暴露 `/embeddings`（兼容 OpenAI）与 `/rerank`。
   - `embedding.py:84` 已按 `base_url + "/embeddings"` 拼端点 → TEI 直接可用，只需把 `MODEL_BASE_URL` 指向 BGE 服务。
   - rerank 需要一个新端点 `/rerank`；`rerank.py` 现有 `LlmReranker`（`rerank.py:81`）走的是 chat completions 打分，**不是 cross-encoder**。新增 `HttpReranker`（POST `/rerank`，body `{query, documents}` → `[{index, score}]`），保留 `LlmReranker` 作为无 BGE 时的降级。
2. **配置拆分**：当前 `MODEL_BASE_URL` / `MODEL_API_KEY` 同时被 chat、embedding、rerank 复用（`config.py:31-32,52-53`）。需**拆出 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`**，否则换了 BGE 后 chat 也会打到 BGE 服务。这是本阶段必须改的配置面。
3. **模型与维度**：`BAAI/bge-m3`（1024 维，多语言，支持稠密+稀疏）；`BAAI/bge-reranker-v2-m3`。注意 `ChunkVectorStore` 集合名按 identity 命名（`milvus_store.py:58`），换 provider 会生成**新集合**，`ensure_collection` 会因维度/identity 不同而重建——需要一次全量重建。
4. **部署形态**：`deploy/docker-compose.yml` 新增 `bge` 服务（GPU 可选，CPU 可跑但慢）；`api`/`worker`/`task-worker` 注入 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL` 指向它。
5. **索引重建**：复用 `knowledge/task_handlers.py` + `recovery.py`，补一个"全量重建"命令 + 幂等校验（重建后 `recall` 不劣化才切换）。
6. **自证**：P2.4 的基线在真实 provider 下重测；`kb-baseline.json` 的 `retrieval_config` 显示 `bge-m3:1024` + `bge-reranker-v2-m3`；报告里给出"dev 实现 vs BGE"的对比。

### P3.2 Neo4j 真多跳推理
- 现状（`knowledge/graph.py`）：只有 `room_type_overview`（一跳）与 `recommend_nearby`（一跳）。
- 补：多跳查询，例如「带投影的房型附近 500m 内有早餐的店」= `RoomType -HAS_AMENITY-> Amenity` + `Hotel -NEARBY-> Place` 的组合推理；把图检索结果作为 KB 召回的**补充腿**（当前图与 KB 检索未合流）。
- **自证**：测试多跳查询命中预期路径；报告里体现"图补充腿"对某些问题的召回增益。

### P3.3 DuckDB 数据湖真分析
- 现状（`data_pipeline/*`、`datalake_query_service.py`）：链路真实但"提速 10-100x"无基准。
- 补：真实业务问题驱动（如"各房型月度入住率与客单价"），给 **可复现的 benchmark 脚本**（对同一批数据比对 PG 直查 vs 预聚合），并把数字写进 README 时标注数据规模。
- **自证**：`deploy/` 下加 benchmark 脚本 + 结果。

**Phase 3 验收**：`docs/eval/kb-baseline.json` 为真实 provider 结果；图谱/数据湖各有测试与基准。

---

## Phase 4 — 生产面

### P4.1 外部系统（只能到模拟器的诚实处理）
- 无法真接微信/门锁/机器人 → **如实叙述**："含 device-sim 契约模拟器（故障注入），为真机联调预留契约"。
- 强化：`docs/contracts/external-systems-v0.md` 补"真实厂商对接 checklist"（微信支付 v3 证书签名、门锁协议差异、设备离线处置），证明想清楚了。
- **门锁/机器人失败处置**：补完整的人工兜底预案（半夜开不了门的 on-call 流程）。

### P4.2 安全加固
- 密钥：`config.py` 去掉默认弱口令（`database_url`/`neo4j_password`/`lock_master_key` 生产必须显式提供，缺失即启动失败）；`deploy/.env` 明确只放占位（现状已是，保持）。
- 端口：`docker-compose.yml` 生产 profile 不把 PG/Redis/Neo4j/MinIO 暴露到宿主；加反向代理 + TLS。
- 限流：加 per-user / per-tenant 配额（现按 IP，`rate_limiter.py:53`）。
- `cors_origins` 默认 `["*"]`（`config.py:101`）→ 生产收紧。

### P4.3 可观测与告警
- Langfuse：支持 self-host 或**强制脱敏后再外发**（当前可能把客人对话含手机号/订单发第三方云）。
- 告警规则：Run 卡死、租约过期率、SSE 断流、DB 连接池耗尽（`runtime.py:41` 固定 4 是硬瓶颈，见 P4.4）。
- SLO + on-call 手册。

### P4.4 可靠性与容量
- checkpoint 连接池固定 4（`runtime.py:40-42`）→ 改为可配置。
- 长会话静默截断（`run_body.py:105` limit=200）→ 显式告警 + 接入摘要（当前摘要阈值 10 万 token 几乎不触发）。
- 审计"每次调用单开事务"（`audit_service.py`）→ 批量/复用连接，降低 DB 往返。
- **报表读路径的权威性（A4 张力）**：`report_service.py` 的 `token_totals` / `token_usage_by_model` / `token_usage_by_day` / `cache_hit_series`（`report_service.py:349/387/417/454`）与 `performance` 的工具计数（`report_service.py:514`）都是"预聚合优先"——只要数据湖存在对应 `agg_*` 分片就不回落到 PostgreSQL，陈旧分片会把"由审计事实派生"静默替换成缓存值（本地实测：残留 `agg_token_daily` 分片令 token 类指标恒为 0，且同窗口 `agg_tool_daily` 分片令 `tool_calls` 恒为 0）。→ 加新鲜度校验，或默认走 PG、仅在显式开关下用预聚合。

### P4.5 备份与合规
- 备份：定时 + 异地 + 恢复演练纳入 CI（`backup.sh` / `restore.sh` 已存在）。
- 合规：PIPL 告知同意、最小必要、出境；客人身份/证件/人脸字段的存储与脱敏方案（当前身份核验、人脸、证件**完全空白**）。

**Phase 4 验收**：一份"上线就绪 checklist"，逐条有证据。

---

## 5. 待确认的决策点

> D1 / D2 / D3 / D5 / D7 已确认（见第 0 节），下表仅列仍需你拍板的项。

| # | 决策点 | 为什么需要你定 | 我的建议 |
| --- | --- | --- | --- |
| D4 | Langfuse 云端 vs 自建 | 合规红线：客人对话含手机号/订单，外发第三方云有 PIPL 出境风险 | 自建（`docker-compose` 加 langfuse 服务），避免客人隐私出境 |
| D6 | 评测集规模上限 | 人工审核成本 | Agent 50+ / KB 150+，先到这规模再评估 |
| D8 | BGE 服务的部署位置 | 决定 CPU/GPU 与 docker-compose 形态 | 与后端同 compose，CPU 起步、GPU 可选 |
| D9 | 是否需要质检 Agent 入图 | 决定是否多一个后台 Agent | 作为独立后台 Agent（不入 chat 图），复用 `qa_auditor_service.py` |

---

## 6. 里程碑与验收总表

| 里程碑 | 交付物 | 可讲的一句话 |
| --- | --- | --- |
| M0（Phase 0） | 配置链路/记忆/脱敏接线 + 测试 | "管理端配置真的生效、记忆真的注入、隐私脱敏覆盖手机号证件号" |
| M1（Phase 1） | Supervisor + 4 子 Agent 图 | "真·多智能体：supervisor 路由 + LangGraph 子图协作" |
| M2（Phase 2） | 真执行器 + 50/150 条评测集 + 真 judge | "评测能自证：50+ 条用例、chunk 级 gold、judge 经人工校准" |
| M3（Phase 3） | 真实 embedding/rerank + 图多跳 + 数据湖基准 | "检索走真实向量+重排，图谱做多跳推理，数据湖有基准数字" |
| M4（Phase 4） | 安全/观测/备份/合规 checklist | "从 demo 到生产就绪的差距清单与逐条证据" |

---

## 7. 简历措辞修订对照（Phase 完成后回填）

| 现在（勿写） | 改造后（可写） |
| --- | --- |
| 「无人酒店多智能体系统」 | 「Supervisor + 子 Agent 的多智能体系统：LangGraph 子图 + Command 路由」 |
| 「recall@10 达 0.9863」 | 「chunk 级 recall@K / top1 命中率（真实 BGE embedding + rerank）」 |
| 「LLM-as-judge 评测体系」 | 「50+ 用例评测集 + 经人工校准的 LLM-as-judge」 |
| 「接入微信支付 / 门锁 / 机器人」 | 「含 device-sim 契约模拟器，为真机联调预留契约与故障注入」 |