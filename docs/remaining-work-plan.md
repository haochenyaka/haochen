# HaoChen 剩余工作计划（Remaining Work Plan）

状态：跟踪表（living document）
日期：2026-10-16
上游：[remediation-plan.md](./remediation-plan.md)（改造总方案）· [remediation-p0-plan.md](./remediation-p0-plan.md)（P0 PR 级方案）· [development-plan.md](./development-plan.md)（原建设计划 P0–P9）

> **编号澄清（勿串）**：本项目有两套「P 编号」。
> - `development-plan.md` 的 **P0–P9**：最初的建设计划，**已全部完成**（验收矩阵 7 条主张全绿）。
> - `remediation-plan.md` 的 **Phase 0–4**：改造计划，即本文件的 P0/P1/P2/P3/P4。
>
> 本文件只管**改造计划**的剩余工作。

---

## 0. 一页速览

```
现在手上 ──→ 硬门槛 ──→ Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 4
   ↑
原定阻塞点两个：① P0 未合进 main   ② CI 不可能绿
—— 两者已于 2026-10-16 全部解除（P0 经 PR #2 合入 main；CI 6/6 全绿）
当前状态：Phase 0–2 已全部交付（P2.1/P2.2 经 PR #5、P2.3/P2.4 经 PR #6 合入 main，`d185fca`）；Phase 3 已全部完成（P3.1 BGE 全量重建 + 169 题基线，P3.2 图多跳补充腿，P3.3 数据湖业务口径与基准）；Phase 4 已关闭（2026-10-18 确认，后续不再安排改造）：P4.2 安全加固、P4.3 可观测告警（2026-10-17）、P4.4 可靠性与容量、P4.5 备份与合规（2026-10-18）已交付，P4.1 的对外表述与真机对接事项转入上线清单跟踪；**补充交付（2026-10-18）**：MCP 运行时真接（PR-M1–M3）与数据管道可靠性补齐（PR-D1–D5），验收 A9/A10 已回填
```

| 层 | 事项数 | 能否立刻推进 |
| --- | --- | --- |
| 第一层：手上未收尾 | 3 | ✅ 已全部收尾 |
| 第二层：硬门槛 | 3 | ✅ #4/#5 已解除；⚠️ #6 待拍板 |
| 第三层：Phase 1–4 | 4 阶段 / 16 子项 | Phase 1–4 ✅ 全部完成（P4.1 改造项已关闭，上线前事项转上线清单） |
| 第四层：零散欠账 | 6 | ✅ 大多可立刻做 |

---

## 第一层　现在手上未收尾的（可立刻做完）

| # | 事项 | 状态 | 细节 |
| --- | --- | --- | --- |
| 1 | **类型/lint 债务清理** | ✅ 已提交 `86a6494`（92 文件），随 PR #2 合入 main | **已完成**：`ruff check` 全绿、`ruff format` 全量（315 文件）、E501 7 处、**mypy 78 → 0**；另修 4 个真 bug（见下方清单）。验证：三项检查绿，单测 264 passed、集成 306 passed |
| 2 | **查 ruff 版本不一致** | ✅ 已查实（**原假设不成立**） | 实测 ruff 0.8.6→88 文件、0.9/0.11/0.14/0.16→85 文件，干净 `main` 亦 90 文件红——**代码从未格式化过**，与版本无关（CI 的 format 步骤自首次提交就存在且未锁版本，故一直红）。结论：**必须格式化**（已完成），锁版本无意义 |
| 3 | **`remediation-p0-plan.md:73` 的过期 ⬜** | ✅ 已清（改为 ✅） | PR-0.1 的「验收矩阵增行」原标着待补，实际 A7 已补进 `acceptance-matrix.md`；仅标记未更新 |

**该分支改动（92 文件 / +1039 −716，已提交 `86a6494`）**，分三类：

1. **全库格式化**：`ruff format` 覆盖 backend 315 文件（含原先列为 19 个的那些）；
2. **类型修复**：`mypy 78 → 0`，根因是把动态 JSONB 列的 `dict[str, object]` 收敛为 `dict[str, Any]`（模型列 + helper 签名），一次性消掉约 55 处级联报错；
3. **真 bug 修复（4 个）**：
   - `data_pipeline/task_handlers.py`：`pq` 未导入（F821）+ `buf` 死代码；`build_aggregates(_ctx)` 参数名与 `TaskHandlerFn` 协议不匹配；
   - `services/langfuse_service.py`：`ssl_ca_certs` 是 v2/v4 都非法的参数 → Langfuse 长期**静默失效**（异常被吞），并锁 `langfuse>=2.55,<3`；
   - `eval/agent/metrics.py`：对 Optional 的 `item.expected` 直接取 `.tool_sequence` / `.task_success`；
   - `services/datalake_query_service.py`：`DuckPyConnection` 拼写错误（应为 `duckdb.DuckDBPyConnection`）。

另：`.github/workflows/ci.yml` 亦在本分支改动内 —— 集成 job **不启 MinIO**（`minio/minio` 已不可匿名拉取），报表/健康面板用例改走「数据湖不可达即按契约降级回 PostgreSQL」路径（见第二层 #5）。

---

## 第二层　三个硬门槛（挡住 P1/P2；#4/#5 已解除，#6 待拍板）

| # | 门槛 | 现状 | 挡住什么 | 解决动作 |
| --- | --- | --- | --- | --- |
| 4 | **P0 未合进 main** | ✅ **已解除（2026-10-16）**：`p0-remediation` 快进到 `2e4578e` → 开 **PR #2** → review → **已合入 main**（merge commit `37a97ce`，17 commit） | ——（已解锁） | —— |
| 5 | **CI 不可能绿** | ✅ **已解除（2026-10-16）**：lint/format/mypy 三项绿；集成 job **不启 MinIO**（dockerhub 拒绝匿名拉取），数据湖/健康面板用例走「不可达即按契约降级回 PostgreSQL」；另**揪出并修复 D7 备份演练自伤共享库**——DROP 掉 `rooms/orders/messages/agent_runs` 后对已删表查行数 → 断言抛错 → `pg_restore` 从未执行 → 会话级共享临时库永久缺表 → 连锁 **66 failed**（修后 CI **6/6 全绿**，integration 304 passed / 3 skipped） | ——（已解锁） | —— |
| 6 | **改造方案仍是 draft** | `remediation-plan.md` 头部标「待评审」，§5 有 4 个未拍板决策 | 影响 Phase 3/4 的形态选择 | 拍板下表 4 个决策 |

### 未拍板决策（`remediation-plan.md §5`）

| 编号 | 决策点 | 为什么需要拍板 | 建议 |
| --- | --- | --- | --- |
| D4 | Langfuse 云端 vs 自建 | 客人对话含手机号/订单，外发第三方云有 PIPL 出境风险 | **自建**（compose 加 langfuse 服务） |
| D6 | 评测集规模上限 | 人工审核成本 | Agent **50+** / KB **150+** |
| D8 | BGE 服务部署位置 | 决定 CPU/GPU 与 compose 形态 | 与后端**同 compose**，CPU 起步、GPU 可选 |
| D9 | 质检 Agent 是否入图 | 决定是否多一个后台 Agent | **独立后台 Agent**，不入 chat 图 |

### P0 已完成内容（本轮改造，供对照）

| PR | 内容 | 状态 |
| --- | --- | --- |
| PR-0.1 | 接通管理端配置链路（`system_prompt`/`model`/`temperature`/`enabled_*` 运行时生效） | ✅ |
| PR-0.2 | 接通长期记忆工位（**只做读+注入**，写入划给 P1.4） | ✅ |
| PR-0.3 | 敏感类别扩默认 + 手机号/身份证「中间打码」（`138****5678`） | ✅ |
| PR-0.4 | 自研中间件理由文档化（`mechanism-baseline.md §3.1`，纯文档） | ✅ |
| PR-0.5a | steer 工位补真（配置驱动、默认关；取消+接力） | ✅ |
| PR-0.5b | tool_approval 接 `interrupt`/`resume`（危险工具挂起等审批） | ✅ |
| 收尾 | 评分 bug 修复（补 `guest_score`/`guest_comment`/`rated_at` 列 + 写入链路） | ✅ |
| 收尾 | 测试基建债（TRUNCATE 隔离 + Redis 限流隔离；集成套件 44 failed → 302 passed） | ✅ |

---

## 第三层　四个改造阶段

### Phase 1 — 多智能体运行时（Supervisor + 子 Agent）

**依赖**：P0 先合进 main（✅ 2026-10-16 已合入 `main`）。**简历最核心的一块。**

- [x] **P1.1 Agent 拓扑**：LangGraph supervisor。✅ 已交付（`app/agents/supervisor/`；多层图 `docs/agent-topology.md` 由编译图渲染，`tests/test_supervisor_graph_shape.py` 做漂移守卫）。
  ```
  START → supervisor（决策节点）
            ├─→ consult_agent（前台咨询子图：KB + 房型 + 时间）
            ├─→ service_agent（客房服务子图：清洁/送物 → 任务派发）
            ├─→ order_agent（订单子图：预订/改期/续订/取消/发票）
            └─→ handoff（投诉/资金/紧急 → 转人工，复用现有 handoff 链路）
                   └─→ END
         supervisor ←── 各子图返回后回到 supervisor 汇总
  ```
  保留单一对外入口（chat），`chat_router`/SSE/`run_worker`/`run_body` 无感。
- [x] **P1.2 共享中间件流水线**：抽 `build_middleware_pipeline` 为共用工厂（当前内联在 `front_desk/agent.py:93`）；11 道工位对子图统一生效。✅ 已交付（工厂落在 `app/agents/supervisor/assembly.py:build_agent_pipeline`，supervisor 与各子图共用）。
- [x] **P1.3 子 Agent 实现清单**：✅ 已交付 4 个必需子 Agent（`front-desk-consult`/`front-desk-order`/`room-service`/`handoff`）+ 后台质检 `qa-auditor`（登记为后台 Agent、Durable Task 驱动、无聊天入口）。
  | 子 Agent | slug | 职责 | 工具来源 |
  | --- | --- | --- | --- |
  | 前台咨询 | `front-desk-consult` | 知识问答、房型、时间 | `knowledge.py` / `system.py` |
  | 订单 | `front-desk-order` | 预订/改期/续订/取消/发票 | `orders.py` |
  | 客房服务 | `room-service` | 清洁/送物/机器人派发 | `room_service.py` |
  | 转人工 | `handoff` | 投诉/资金/紧急 → 工单 | `conversation_service` handoff |
  | 后台质检 | `qa-auditor` | 后台跑批质检，不入 chat 图（`chat_entry=False`） | `qa_auditor_service.py` |
- [x] **P1.4 记忆写入链路**（承接 P0.2）：✅ 已交付。新增 `app/memory/`（`extraction` / `service` / `task_handlers`）。Run 以 completed 收敛时 `RunExecutor._finalize` **同事务**提交 `memory.extract` 后台任务、提交后投递到 task 队列；处理器复核 Run 终态 → 找 Guest → 读对话 → LLM 结构化抽取 → 规则校验（分类白名单/长度/敏感明文/去重/上限）→ 写 `GuestMemory`（`source="auto:memory.extract"`）。默认关（`memory_extract_enabled=False`，无密钥 no-op）。测试：单测 9 例 + 集成 4 例。
- [x] **P1.5 注册表与配置**：子包自动注册（`app/agents/toolkits/registry.py:41` 已支持）+ slug 命名 + 管理端列表展示。✅ 已交付并实测：`discover_agents()` 返回 6 个 slug（`front-desk` + 4 子 Agent + `qa-auditor`），各带 `config_items`；`admin_agent_config_router` 的 `list_agents` → 前端 `AgentConfigView.vue` 动态渲染（无硬编码 slug 过滤）。管理端为扁平列表，未按 supervisor/子 Agent 分组（非验收项）。

**验收**：可 mermaid 出图的多层图（✅ `docs/agent-topology.md`，`test_supervisor_graph_shape.py` 做漂移守卫）；端到端复合请求（「我要订房 + 顺便叫一份早餐到房间」）被 supervisor 正确拆分到 order + service（✅ 真实编译图 e2e：`tests/integration/test_agent_runtime.py::test_supervisor_route_facts_and_sub_agent_attribution`）；审计事实里能看到路由决策与子 Agent 归属（✅ 已达成，见下）。

**P1 审计归属交付**（承接验收第三项）：新增审计事实表 `route_audit`（迁移 `20261014_0023`，`down_revision=20261013_0022`），字段 `decided_route` / `dispatched_route` / `source`（llm|keyword）/ `reason` / `agent_slug='supervisor'`；supervisor 决策节点在派发前落一条，判定值与授权改派后的实际派发目标都可复盘（收尾时 `dispatched_route` 为 NULL）。子 Agent 归属改为**逐子图派生记录器**（`AuditRecorder.with_agent(slug)`，`build_loop_subgraph(agent_slug=...)` 取 `LoopSubAgent.slug`）：模型与工具事实按实际执行的子 Agent 记 slug（不再一律记入口 slug `front-desk`）；共享流水线仍是单例，token 用量与工位行为不变。测试：单测 +1（归属派生）、真实图集成 +1（路由事实与归属）。

**P1 归属与计量补全**（收口两处已登记边界）：① **SSE 事件包络**——`agent_slug` 语义改为**实际执行者**，新增 `entry_slug` 保留入口 Agent：子图内发出的事件（`message.delta` / `tool.call` / `tool.result`）归到具体子 Agent，Run 级合成事件二者相等；派生视图 `RunEventEmitter.with_agent(slug)` 共享同一 `seq` 游标与首 token 埋点，归属随装配期确定（`docs/mechanism-baseline.md` §2 事件协议行已同步）。② **supervisor 路由的 LLM 调用**——结构化输出改带 `include_raw=True` 取回原始消息，按 `AuditRecorder.with_agent('supervisor')` 写 model_audit（成功记用量、失败留痕），用量经 `MiddlewarePipeline.record_side_channel_usage` 并入 Run 汇总；该调用不经流水线（不注入记忆 / 技能 / steer），解析失败仍落关键词兜底但用量照记。测试：单测 +4（事件派生器归属与共享 seq；路由审计成功 / 解析失败 / 调用失败 / 未接线）、真实图集成断言事件归属。

**风险 / 待定**：子 Agent 粒度是否按上表（已按上表定稿）；supervisor 决策用 LLM 结构化输出 vs 规则兜底——**已定稿为「LLM 为主 + 关键词兜底」**，且 LLM 路径已接入审计与用量计量。

**后台质检 Agent 交付**（`qa-auditor`，承接 D9「独立后台 Agent，不入 chat 图」）：登记为第 6 个内置 Agent（`app/agents/buildin/qa_auditor/`），自带可配置项（扫描窗口 / 文档过期阈值 / 低召回判定阈值），配置由跑批任务读取后构造 `QaAuditorService`。运行由 **Durable Task `qa.audit` v1** 驱动（`app/quality/task_handlers.py`）：task worker **小时级 cron** 提交（小时级去重键，重启补发不堆积），管理端 `GET /admin/quality/audit` 仍为**同步触发**（不建任务，直接跑一轮），两者共用同一服务。**新增接入侧边界 `chat_entry`**：`BaseAgent.chat_entry`（默认 `True`）标记非聊天入口 Agent；`qa-auditor` 置 `False`，`chat_router` 在建 Run 之前即拒绝客人指定该 slug（400），避免建出无可用图的 Run——未注册 slug 不做此判定，仍走既有路径。跑批失败不自动重放（标注幂等，下次调度补齐）。测试：Agent 注册/配置/无图 3 例、任务注册与去重键 4 例、cron 契约 1 例；集成 `qa.audit` 提交→执行→标注落库 + 同键去重 + 接入侧拒绝。

---

### Phase 2 — 评测体系真跑通

**依赖**：P1 完成（数据集工具链校准依赖 Agent 拓扑）。**可提前做**：P2.1 + P2.2 的工具名对齐测试。

- [x] **P2.1 真实执行器**：✅ 已交付 `RealCaseExecutor`（线程/消息/请求 → 队头 Run → `RunExecutor` → `tool_audit` 回读），支持同线程多轮回放并回读末轮回答；`ScriptedCaseExecutor` 仅留测试，管理端注入真实执行器。
- [x] **P2.2 数据集重建（LLM 生成 + 人工审核）**：✅ Agent 评测集 8 → **61 条**（checkin 12 / orders 18 / dispatch 7 / handoff 6 / multi_turn 6 / composite 5 / edge 7，含 20 条多轮）；KB 评测集 73 → **169 题**，gold 升级为 `(document_title, ordinal)` 分块级锚点；人工审核记录在 `docs/eval/review/`；工具名已对齐并加防漂移守卫 `test_eval_dataset_tools_registered`。
- [x] **P2.3 LLM-as-judge 真实现**：✅ 四维评分（相关性/事实性/语气/边界，0–1 + 理由）；评判温度 0.7（生成侧默认 0.2，可用 `AGENT_JUDGE_MODEL_NAME` 换模型）；自一致性采样 3 次逐维取中位；20 条人工校准集 + Cohen's kappa（产物 `docs/eval/agent-judge-calibration.json`，本次 **kappa=1.0**）；评判指标与逐题明细已写入报告，评判器身份进配置指纹。
- [x] **P2.4 指标诚实化**：✅ 无答案评判时 KB `overall_score` 主指标切到 `chunk_recall@K`（无分块 gold 才退回文档级 `recall@K`）；README 已更新为 169 题、分块级 gold；P2 交付时为开发态 embedding 口径（`chunk_recall@10` 0.9675 / `primary@1` 0.6036），真实 BGE-M3 数字已于 P3.1 重测（0.9941 / 0.6746）。

**验收**：✅ `pytest -m eval` 真跑通（61/61，报告 ID 39–45）；评判覆盖 61/61、四维均分 0.9622、单题通过率 0.9016；报告落库且可复现（指纹机制含执行器与评判器身份）；README 评测段落数字均可追到报告或校准产物。

**交付记录**：P2.1/P2.2 经 **PR #5** 合入 main（merge commit `767b493`）；P2.3/P2.4 及 edge-001 根因修复经 **PR #6** 合入 main（merge commit `d185fca`，CI 6/6 全绿）。评判器已有 `use_judge` 开关，脚本模型集成测试显式关闭，避免误打真实 API。

---

### Phase 3 — 检索真实性

**根因**：`MODEL_BASE_URL=https://api.deepseek.com/v1` 只有 chat 端点，DeepSeek **不提供 embedding / rerank** → P3.1 前生产配置下 `build_embedding_provider()`（`knowledge/embedding.py`）与 `build_reranker()`（`knowledge/rerank.py`）必然落回 dev 实现（旧 `kb-baseline.json` 因此是 `"embedding": "dev-deterministic:64"`、`"reranker": "keyword"`）。P3.1 已拆出独立 `EMBEDDING_*` / `RERANK_*` 端点。

- [x] **P3.1 真实 embedding + rerank（本地 BGE-M3 + bge-reranker-v2-m3）**：
  - **Spike 已完成（2026-10-17）**：[spikes/2026-10-17-bge-feasibility.md](spikes/2026-10-17-bge-feasibility.md)。TEI CPU 可用于 embedding（查询 p50 0.122s、索引约 140–158 token/s）；但真实分块 rerank 23 候选需 53–59s，**CPU 不适合在线同步链路**。本阶段默认只落 embedding，rerank 保留 `LlmReranker`/`KeywordReranker` 降级链；另需对齐 TEI `--max-client-batch-size` 与后端 `EMBED_BATCH_SIZE=32`。
  - **交付记录（2026-10-17）**：已落地 `EMBEDDING_*`/`RERANK_*` 配置拆分、`HttpReranker`（TEI `/rerank`，失败降级）、compose `bge-embed`（knowledge profile）与 `bge-rerank`（独立 profile）、`deploy/prepare_bge_models.py` 及单测；完成真实 TEI 的 provider 级联调（embedding 1024 维、rerank 相关片段排第 1）。全量重建 20 篇文档 / 95 个分块至 `haochen_kb_bge_m3`，169 题评估落库报告 ID 4：`chunk_recall@10` **0.9941**、`primary@1` **0.6746**（旧基线 0.9675 / 0.6036，报告 2/3 已作废）。
  - BGE 服务选型：`text-embeddings-inference`(TEI) 或 `Infinity`，暴露 `/embeddings`（兼容 OpenAI）与 `/rerank`。
  - rerank 需新端点 `/rerank`；现有 `LlmReranker`（`rerank.py:81`）走 chat completions 打分，**不是 cross-encoder** → 新增 `HttpReranker`（POST `/rerank`），保留 `LlmReranker` 作降级。
  - **配置拆分（本阶段必须改）**：当前 `MODEL_BASE_URL`/`MODEL_API_KEY` 同时被 chat/embedding/rerank 复用 → 拆出 `EMBEDDING_BASE_URL` / `RERANK_BASE_URL`。
  - 模型维度：`BAAI/bge-m3`（1024 维）；`ChunkVectorStore` 集合名按 identity 命名（`milvus_store.py:58`），换 provider 会生成新集合，需一次**全量重建**。
  - 部署：`deploy/docker-compose.yml` 新增 `bge-embed`（默认启用）与 `bge-rerank`（显式开关）；`api`/`worker`/`task-worker` 注入新 URL。
  - 索引重建：复用现有 `reprocess` → `kb.parse` → `kb.index` 任务链完成全量重建，未新增独立 CLI；重建前先启动 `bge-embed`，重建后核对 Milvus 集合行数与 PG 分块数一致。
  - 已知问题：首次创建集合时多个索引任务并发执行，偶发 `collection not loaded`（本次 1/20 文档，重试后成功）；后续可在删除/写入前补集合加载幂等或对瞬时错误重试。
- [x] **P3.2 Neo4j 真多跳推理**：
  - **交付记录（2026-10-17）**：`graph.py` 新增 `match_room_types`（设施全命中 AND 语义）、`recommend_nearby` 距离上限与 `multi_hop_insight`（房型两跳 + 周边一跳组合，limit=0 可跳过单腿）；新增 `graph_retrieval.py` 做确定性条件抽取、编号渲染与降级。`KnowledgeRetriever` 把图事实并入上下文，KB 片段与图事实共用连续编号、引用以 `source=graph` 区分；`KB_GRAPH_ENABLED` 默认开启，`KB_GRAPH_TIMEOUT_SECONDS` 默认 3s，图故障/超时只丢弃图腿，不影响 KB 检索；`query_kb` 在仅有图事实时不再误判“无相关资料”。
  - **自证**：[eval/graph-leg.md](eval/graph-leg.md)。真实 Neo4j 上验证：投影房型 + 500m 餐饮组合、儿童用品唯一命中、5 分钟步行排除 6 分钟地铁、500m 内药店返回空；普通文档问题不触发图腿。测试覆盖单测、真实图集成与 KB/图合流。
  - **已知限制**：抽取为关键词/正则，不理解未登记的同义改写；仅覆盖已建模关系（房型设施、周边类别/距离/步行时间）；周边为样例数据，上线前需替换为门店实测值。
- [x] **P3.3 DuckDB 数据湖真分析**：
  - **交付记录（2026-10-17）**：新增 `business_aggregates.py` 统一业务口径（订单按 `[check_in, check_out)` 逐夜展开、跨月按夜归属、取消不计；入住率 = 已售房晚 /（房间数 × 当月天数），客单价 = 房费 / 已售房晚）；新增 `data_pipeline.export_business` 导出订单（`updated_at` 键集增量 + 版本去重）与房型/房间快照；`build_aggregates` 增生成 `agg_room_type_monthly`；管理端新增 `GET /admin/datalake/analysis/room-type-monthly`。
  - **可复现基准**：`backend/bench_datalake.py` 固定 seed 生成合成订单，同一口径分别走 PostgreSQL 临时表直查与 DuckDB 预聚合，逐字段比对一致。20 万订单 / 42.5 万入住夜：中位 **568.43ms → 1.51ms（约 378x）**；5 千订单冒烟为 34.00ms → 1.80ms（18.8x，倍数随规模增长）。
  - **自证**：[eval/datalake-benchmark.md](eval/datalake-benchmark.md) + `eval/datalake-benchmark.json`（含环境、规模、min/p95、一致性）；README 提速声明按此改写并标注数据规模。
  - **已知限制**：基准不含导出/构建预处理与 MinIO 下载耗时；合成随机分布、单机 warm cache；即席探索仍需走 `/admin/datalake/query` 直查。

**验收**：✅ `docs/eval/kb-baseline.json` 为真实 provider 结果（`retrieval_config` 显示 `bge-m3:1024`），`chunk_recall@10` 0.9941、`primary@1` 0.6746；rerank 因 CPU 延迟保留 `keyword`，`bge-reranker-v2-m3` 作为显式开关。P3.2 图谱已有真实多跳测试与定向自证（[eval/graph-leg.md](eval/graph-leg.md)）；P3.3 数据湖已有真实业务问题口径与可复现基准（[eval/datalake-benchmark.md](eval/datalake-benchmark.md)，20 万订单 378x，结果一致）。

---

### Phase 4 — 生产面

- [x] **P4.1 外部系统（只能到模拟器的诚实处理）**（2026-10-18 关闭）：无法真接微信/门锁/机器人 → 如实叙述「含 device-sim 契约模拟器（故障注入），为真机联调预留契约」；`docs/contracts/external-systems-v0.md` 补「真实厂商对接 checklist」（微信支付 v3 证书签名、门锁协议差异、设备离线处置）；补门锁/机器人失败的人工兜底预案（半夜开不了门的 on-call 流程）。Phase 4 改造项至此关闭，后续不再安排改造；真实厂商对接与 on-call 兜底作为上线前运营项在 [上线就绪清单](operations/launch-readiness-checklist.md) 跟踪。
- [x] **P4.2 安全加固**（2026-10-17 交付）：
  - **生产配置守卫**：`ENV=production` 时，`DATABASE_URL`/`NEO4J_PASSWORD`/`MINIO_SECRET_KEY`/`LOCK_MASTER_KEY`/`DEVICE_SIM_SIGNING_SECRET` 仍为开发默认值或弱口令、`CORS_ORIGINS` 为空/通配、`DEV_IDENTITY_ENABLED=true`、关闭限流，任一命中即 `create_app()` 启动期抛 `RuntimeError` 拒绝启动（`Settings.production_issues` / `validate_production`）。
  - **CORS 收紧**：通配符来源下自动关闭 `allow_credentials`（浏览器规范），生产必须显式配置具体域名。
  - **限流分层**：Bearer 会话经 Redis 解析到用户，已认证请求同时计入「用户 + IP」两个滑动窗口；新增部署级租户总额度（分组限额 × `RATE_LIMIT_TENANT_MULTIPLIER`，默认 10，0 关闭）；`X-Forwarded-For` 仅在 `RATE_LIMIT_TRUST_FORWARDED_FOR=true` 时采信。
  - **生产部署形态**：新增 `deploy/docker-compose.prod.yml` 覆盖层，PG/Redis/MinIO/Milvus/Neo4j/device-sim/API 一律不发布宿主端口，仅 nginx 暴露 80/443；密钥用 `${VAR:?}` 强制显式提供。nginx 模板提供 TLS 1.2/1.3、HTTP→HTTPS、HSTS 与安全头、SSE 关闭缓冲，并覆盖写入 XFF（防伪造）。
  - **验证**：ruff / mypy 全绿，后端单测 392 passed；本地集成套件 325 passed / 3 skipped（D7 本机无 `pg_dump` 必然 skip，仍以 CI 为准）；base 与 prod compose `config --quiet` 通过；nginx 模板 `nginx -t` 通过。
- [x] **P4.3 可观测与告警**（2026-10-17 交付）：
  - **Langfuse 外发边界**：默认按外部托管处理，只配密钥不初始化；`LANGFUSE_SELF_HOSTED=true` 走内网自建，或 `LANGFUSE_ALLOW_EXTERNAL_EXPORT=true` 显式允许外发。外发模式下用户/会话标识经 SHA-256 脱敏、score 自由文本备注丢弃；self-host 保留原值便于排障。生产环境配置密钥但两项均未声明时，启动期拒绝启动。
  - **SSE 指标**：流生命周期、客户端断开、服务端异常、事件总线读写失败写入 Redis 分钟桶（TTL 1h，多副本聚合）；Redis 不可用时降级为进程内计数且不阻断流。跟随/回放失败即计入异常收流，SSE 仍按数据库投影兜底。
  - **告警规则**：`GET /api/admin/health/alerts`（admin/manager 只读）重算四类规则——`run_stuck`（pending > 300s 或 running 心跳 > 180s）、`lease_expiry_rate`（15min 内 ≥10 次已结束尝试且过期率 >5%）、`sse_disconnect`（15min 内 ≥20 次订阅且异常率 >10%，或事件总线读写失败）、`db_pool_exhaustion`（本进程签出率 ≥90%）。返回 `ok/warning/critical` 与可定位 `facts`，阈值全部可配置。
  - **SLO + on-call**：新增 `docs/operations/slo-oncall.md`，含 SLO 口径、告警阈值、逐规则处置 SQL/动作、升级路径、季度演练与已知限制（拉取式评估、池指标单进程、指标 TTL）。
  - **验证**：ruff / mypy 全绿；单元 400 passed；本地集成 326 passed / 3 skipped；base 与 prod compose `config --quiet` 通过。
- [x] **P4.4 可靠性与容量**（2026-10-18 交付）：
  - **checkpoint 连接池可配置**：`CHECKPOINT_POOL_MIN_SIZE` / `CHECKPOINT_POOL_MAX_SIZE` / `CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS`（默认 1 / 4 / 10s）；`Settings` 启动期与 `resolve_checkpoint_pool_settings` 解析期双重校验，就绪日志输出实际池容量。
  - **长会话截断显式化并接入摘要**：新增 `MESSAGE_HISTORY_LIMIT`（默认 200），只加载最近消息并多取一条判断截断；命中时记录 thread/run 告警并向模型注入显式缺失提示。原实现 `list_for_thread` 取的是**最早** 200 条，长会话会静默丢掉最新消息，本次一并修正；记忆抽取同样改取最近 20 条。摘要阈值默认 100K → **24K**，低于常见 32K/64K 上下文窗口，压缩会真正触发。
  - **审计合并写入**：`AuditRecorder` 把同一短窗口内的并发事实合并为一次事务（`AUDIT_BATCH_WINDOW_MS` 默认 5ms、`AUDIT_BATCH_MAX_FACTS` 默认 32；窗口 0 恢复逐条提交），降低 DB 往返；每个调用仍等待所属批次提交成功才返回，保持「已发生事实不丢」。批次按 model→tool→route 固定顺序写入，避免跨 Run 咨询锁成环。
  - **验证**：ruff / mypy 全绿；单元 419 passed；本地集成 327 passed / 3 skipped（D7 本机无 `pg_dump` 仍 skip，以 CI 为准）；base compose `config --quiet` 通过。
- [x] **P4.5 备份与合规**（2026-10-18 交付）：
  - **备份定时 + 异地 + 加密**：`deploy/backup.sh` 重写为可调度脚本，支持 `BACKUP_INTERVAL_SECONDS`（默认 86400）、`BACKUP_RETENTION_DAYS`（默认 30）、AES-256-CBC(pbkdf2) 加密、SHA-256 校验、manifest 与异地副本；compose 新增 `backup` 服务，生产覆盖层用 `${VAR:?}` 强制 `BACKUP_ENCRYPTION_KEY` 与 `OFFSITE_BACKUP_DIR`，并要求 `BACKUP_REQUIRE_ENCRYPTION/OFFSITE=true`。
  - **恢复**：`deploy/restore.sh` 支持 `--yes` 非交互、自动识别 `.enc/.gz`、校验和、恢复前确认、恢复到指定新库、`--exit-on-error`，并按 `RESTORE_VERIFY_TABLES` 校验关键表存在与行数。
  - **CI 演练**：新增 `backup-restore` job，真实执行加密备份 + 异地副本 + 过期清理，随后 `DROP TABLE` 模拟丢失并用 `restore.sh` 恢复到新库、断言行数一致。
  - **PIPL 告知同意**：新增公开 `GET /api/privacy/notice`、登录态 `GET/POST /api/privacy/consents`；`consent_records` 追加式事件表（迁移 `20261018_0025`），支持同意/撤回、同状态去重、政策版本校验（409）与未知用途拒绝（422）；敏感用途（身份核验、人脸、营销）单独列出。
  - **合规方案**：`docs/operations/privacy-compliance.md` 覆盖数据清单、最小必要、出境、证件/人脸存储与脱敏方案、权利响应与缺口；新增 PII 明文字段守卫测试，禁止 `id_card*`、`face_image/embedding` 等列进入模型。
  - **上线 checklist**：`docs/operations/launch-readiness-checklist.md` 逐条列证据并明确 P4.1、前端同意页、服务端同意强制、自动化保留期清理、MinIO 备份与供应商合规等硬缺口；`docs/operations/backup-restore.md` 提供 RPO/RTO、调度、监控与恢复 runbook。
  - **验证**：ruff / ruff format / mypy 全绿；单元 **425 passed**；本地集成 **331 passed / 3 skipped**；base 与 prod compose `config --quiet` 通过；`bash -n` 脚本语法通过。

**验收**：一份「上线就绪 checklist」，逐条有证据。

---

### Phase 4 补充 — MCP 运行时与数据管道可靠性（2026-10-18 交付）

> 上游方案：[remediation-mcp-plan.md](./remediation-mcp-plan.md) · [remediation-data-pipeline-plan.md](./remediation-data-pipeline-plan.md)。验收证据见 [acceptance-matrix.md](./acceptance-matrix.md) A9/A10。

- [x] **PR-M1 MCP 客户端与连接测试**：`app/agents/mcp/`（client/probe）；传输白名单（拒绝 `stdio`）、配置哈希缓存、凭证仅从 `MCP_<SLUG>_API_KEY` 注入；管理端 `POST /{id}/test`、`GET /{id}/tools`。测试：单测 8 + 集成 5。
- [x] **PR-M2 运行期接线**：`AgentConfig.enabled_mcp_servers`（迁移 0028）；`run_body` 构造 `mcp_server` 资源快照；装配期按启用 slug 装载工具、加 `{slug}__` 前缀、默认危险（`read_only_tools` 豁免）、`disabled_tools` 剔除；Run 级注册表不污染进程级；经同一 `build_tool_node` 获得审计/打码/审批。测试：单测 5 + 集成 2。
- [x] **PR-M3 mcp-gateway 独立服务**：`app/mcp_gateway/` 以子路径暴露 `/lock/mcp`、`/robot/mcp`、`/wechat/mcp`，复用 `LockClient` / `RobotClient` / `WechatClient`（内部走 device-sim）；compose 新增 `mcp-gateway` 与健康检查；seed 预置三条 `mcp_servers`。测试：网关 12 + 渠道契约 2 + 种子集成 1。
- [x] **PR-M4 验收与文档**：A9 回填；§2.7 与 design/README 按交付事实更正。
- [x] **PR-D1 运行记录可靠性**：修 checkpoint 键、预聚合静默返回、归档主键/分批/失败语义、run/task 事务竞态；同规则并发互斥。测试：handler 单测 4 + 集成 5。
- [x] **PR-D2 规则种子与参数化**：9 条默认规则 + `config` 参数编辑；`task_type` 白名单与 cron 保存校验。测试：集成 4。
- [x] **PR-D3 真实调度器**：task-worker 每分钟扫描 `schedule_cron`，`croniter` 命中判定 + 分钟级去重键提交 `scheduled` run。测试：调度单测 5 + 集成 1。
- [x] **PR-D4 数据湖前端页**：管道页新增「数据湖」tab（分区浏览/预览/只读 SQL/房型月度指标），湖不可达显式降级。
- [x] **PR-D5 报表预聚合新鲜度守卫**：`REPORT_AGG_ENABLED` 默认关（走 PG）；开启时校验分片新鲜度，陈旧自动回落 PG，不再静默归零。测试：降级 4 例。
- [x] **PR-D6 验收与文档**：A10 回填；§2.15 与 README 更新。

---

## 第四层　零散欠账与已知问题

| # | 事项 | 来源 / 证据 | 备注 |
| --- | --- | --- | --- |
| 7 | **`test_room_service` 全局注册表串扰** | `remediation-p0-plan.md:90` 明标「未修」 | `lock_rotation`/`room_service` 共享模块级 lock-task-handler 注册表（`importlib.reload` + 模块级 `LockClient` 注入）致替身状态串扰；需改测试夹具，独立于本轮改动 |
| 8 | **PR-0.5b 三个已知边界** | `remediation-p0-plan.md:260-263` | 单审批/单 interrupt、执行体可重入、崩溃安全——多审批留后续 |
| 9 | **`qa_auditor_service.py:174` 标题相似度是占位** | 代码注释「按标题关键词简单分组」 | 冲突检测的相似度算法待补 |
| 10 | **`graph_seed.py` 周边地点是虚构样例** | 代码注释「正式上线前必须替换」 | Neo4j 种子数据 |
| 11 | **`knowledge/seed.py` 知识库是占位内容** | `KB_DESCRIPTION = "样例知识库…替换为真实资料后重新上传"` | 需换真实酒店资料 |
| 12 | **`vision_api` 是 mock** | `vision_api/__init__.py:1` — 人脸/工牌/工服**恒返回通过**，真实视觉服务留 P9 | 与 `remediation-plan.md` 第 14 项同源，属**已接受的现状**（第 0 节 D-P4：外部系统只有模拟器） |

---

## 5. 建议执行顺序

```
① ✅ 查 ruff 版本（已查实：非漂移，必须 format）
② ✅ 收尾 cleanup PR：ruff check + ruff format + mypy（均绿）→ 已提交 `86a6494`
③ ✅ 开 P0 的 PR #2 → CI 6/6 全绿 → 已合入 main（merge commit `37a97ce`）
④ ✅ 提交 P1 分支（`p1-multi-agent`，三条验收已全达成）→ 提交 `c58ce91` + 收尾 `70cff14`，经 **PR #3** 合入 main（`b75bc6c`）
   └ 本轮收尾：SSE 事件包络按执行者归属（✅ 已完成）、supervisor 路由调用接审计与用量（✅ 已完成）、`qa-auditor` 登记为后台 Agent 并 Task 化（✅ 已完成），P1 全部收口
   └ 可并行：P2.1 真执行器 + P2.2 工具名对齐测试（不依赖 P1）
⑤ ✅ P2 全量（PR #5 `767b493` → PR #6 `d185fca`）→ ✅ P3.1 BGE 可行性 spike（2026-10-17）→ ✅ P3.1 实现 + 全量重建 + 169 题基线（2026-10-17）→ ✅ P3.2 图多跳与补充腿（2026-10-17）→ ✅ P3.3 数据湖业务口径与基准（2026-10-17）→ P4
```

**一句话总结**：原定**阻塞点两个**——**P0 未合进 main**、**CI 不可能绿**——**均已于 2026-10-16 解除**：P0 以 **PR #2** 合入 `main`（merge commit `37a97ce`）；CI 在 lint/format/mypy 转绿、集成 job 明确「**不启 MinIO + 数据湖按契约降级**」、并**修掉 D7 备份演练自伤共享库**（曾连锁 66 failed）后 **6/6 全绿**。P1 经 **PR #3** 合入 `main`（`b75bc6c`）；P2 经 **PR #5**（`767b493`）与 **PR #6**（`d185fca`）合入 `main`，**Phase 0–2 已全部完成**。P3.1 已交付：**本地 BGE-M3 embedding 可用，CPU rerank 不进入在线链路**；169 题基线 `chunk_recall@10` 0.9941、`primary@1` 0.6746（详见 spike 报告与 `docs/eval/kb-baseline.json`）；P3.2 图多跳补充腿与 P3.3 数据湖基准均已完成（四舍五入自证见 `docs/eval/`）；下一入口为 **P4**。Phase 4 是可立即启动的「内容工作」，不受阻塞。

---

## 6. 里程碑对照（`remediation-plan.md §6`）

| 里程碑 | 交付物 | 可讲的一句话 |
| --- | --- | --- |
| M0（Phase 0）✅ | 配置链路/记忆/脱敏接线 + 测试 | 「管理端配置真的生效、记忆真的注入、隐私脱敏覆盖手机号证件号」 |
| M1（Phase 1） | Supervisor + 4 子 Agent 图 | 「真·多智能体：supervisor 路由 + LangGraph 子图协作」 |
| M2（Phase 2） | 真执行器 + 50/150 条评测集 + 真 judge | 「评测能自证：50+ 条用例、chunk 级 gold、judge 经人工校准」 |
| M3（Phase 3）✅ | 真实 embedding/rerank + 图多跳 + 数据湖基准 | 「检索走真实向量+重排，图谱做多跳推理，数据湖有基准数字」 |
| M4（Phase 4） | 安全/观测/备份/合规 checklist | 「从 demo 到生产就绪的差距清单与逐条证据」 |