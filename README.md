# 酒店 Multi-Agent 系统

> 面向无人酒店的 AI Agent 全栈系统：微信小程序（客人 / 保洁）与 Web 管理后台三端一体，由 LangGraph 多智能体运行时驱动咨询、预订、入住、客房服务、退房改密与质检的全流程。

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0-1C3C3C)](https://langchain-ai.github.io/langgraph/)
[![Vue](https://img.shields.io/badge/Vue-3-42B883?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](./deploy/docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

酒店场景的对话式服务很容易停留在「能聊天」的 Demo：问答之外，订单、门锁、派单与审计各自为政。本项目把 Agent 放进真实业务链路——自然语言请求由 supervisor 派发给领域子 Agent，通过受权限约束的工具完成下单、发凭证、派清洁、叫机器人等副作用，并以 PostgreSQL 中的审计事实作为运营报表与评估的唯一权威来源。三端共用同一套运行时与存储，权限、状态机与失败语义保持一致。

## 项目状态

- **建设阶段**：P0–P9 已全部完成，验收矩阵 10 条主张全绿（61 个正例 + 44 个负例），见 [验收矩阵](docs/acceptance-matrix.md)。
- **改造阶段**：Phase 0–4 已全部完成，后续不再安排改造。
- **近期补充**：MCP 运行时真接（M1–M4）与数据管道可靠性整改（D1–D6）已交付，验收 A9 / A10 已回填。

## 核心能力

| 能力域 | 说明 |
| --- | --- |
| 多智能体编排 | `supervisor` 按意图与资源可见性派发到咨询 / 订单 / 客房服务 / 转人工 4 个子 Agent；单 Run 最多派发 4 次，决策以结构化输出为主、关键词兜底，全过程落 `route_audit` |
| 三端业务闭环 | 客人自助咨询、预订、入住、退房；保洁接收派单、留证、改密确认与打卡；管理端完成配置、监控、审批、评估与报表 |
| 混合检索知识库 | Milvus 稠密向量 + BM25 稀疏检索，叠加 Neo4j 关系图补充腿，经 Rerank 精排；169 题分块级黄金锚点 `chunk_recall@10` **0.9941**、`primary@1` **0.6746** |
| 评估闭环 | 知识库评估 + 生产同源 Agent 评估 + LLM-as-judge 四维评分；报告只追加、配置指纹变化自动作废旧报告；Langfuse 提供链路追踪 |
| 数据平台 | 清洗管道 + 数据湖：MinIO Parquet + zstd、DuckDB 直读预聚合；20 万订单下同一查询中位耗时 **568ms → 1.5ms（约 378x）**，预聚合缺失 / 陈旧时自动降级回 PostgreSQL |
| MCP 工具运行时 | 管理端注册、启停、探测远程 MCP（`streamable_http` / `sse`，拒绝 `stdio`）；Run 装配期按启用的 server 注入工具，单工具可禁用，副作用工具默认走审批 |
| 工程底座 | API / Worker 分离、异步任务隔离、不透明会话令牌、资源级授权、幂等与审计、生产覆盖层校验、备份恢复演练 |

## 多智能体编排

入口 Agent `front-desk` 装配 supervisor 图，对外契约仍是单一编译图：输入历史消息，输出消息与 token 用量；差别在图内部是多层拓扑——supervisor 决策后派发子图，子图产出后回到 supervisor 决定是否继续派发。

```mermaid
flowchart LR
    U["用户消息"] --> FD["front-desk<br/>入口 Agent"]
    FD --> SUP["supervisor<br/>派发决策"]
    SUP --> C["front-desk-consult<br/>咨询 / 房型"]
    SUP --> O["front-desk-order<br/>预订 / 改期 / 取消 / 发票"]
    SUP --> R["room-service<br/>清洁 / 送物 / 机器人"]
    SUP --> H["handoff<br/>投诉 / 退款 / 赔偿"]
    C & O & R & H --> SUP
    SUP -.-> END["结束 Run"]
```

| 节点 | 职责 |
| --- | --- |
| `supervisor` | 读取意图与当前资源快照，决定派发目标或结束；模型不可用时关键词兜底 |
| `front-desk-consult` | 知识问答、房型与时间咨询，默认派发目标；工具以只读检索为主 |
| `front-desk-order` | 预订、改期、续订、取消、发票等订单事务 |
| `room-service` | 清洁派单、物品配送、机器人配送 |
| `handoff` | 投诉、退款、赔偿 → 生成待处理工单并转人工 |
| `qa-auditor` | 离线质检 Agent（无聊天入口）：扫描增量会话，产出质检标签、风险告警与数据质量信号 |

派发语义：

- 一次 Run 内可多次派发，复合请求逐个处理；上限 `MAX_DISPATCHES = 4`，避免决策与派发互相触发空转。
- 决策以 LLM 结构化输出为主，失败或不可用时落关键词兜底；判定来源记为 `llm` 或 `keyword`。
- 授权感知：若目标子 Agent 在当前资源快照下没有可见工具，而其他子 Agent 有，则改派后者。
- 每次决策先写 `route_audit`（判定值、来源、实际派发目标）；决策本身调用模型时，用量归到 `supervisor`。
- 子图内事件归属具体子 Agent，`entry_slug` 恒为 Run 入口 Agent。
- 拓扑图由编译后的 LangGraph 图直接渲染，`tests/test_supervisor_graph_shape.py` 保证文档与代码不漂移，详见 [多智能体运行时拓扑](docs/agent-topology.md)。

## 三端与业务闭环

| 端 | 载体 | 核心能力 |
| --- | --- | --- |
| C 端客人 | 微信小程序（uni-app） | 酒店咨询、房型与房态、预订 / 改期 / 取消 / 续订、支付、发票、客房需求（送物 / 清洁）、退房、投诉、周边推荐 |
| 保洁员工 | 微信小程序（uni-app） | 任务接收与执行、退房清洁留证与改密确认、上下班打卡 |
| 管理后台 | Web（Vue 3 + Ant Design Vue） | 运营驾驶舱、Agent 配置、会话监控与人工接线、审批、MCP 服务管理、数据管道与数据湖、知识库 / Agent 评估、质量评分卡、数据报表、智能体日志、记忆管理、运行健康 |

典型闭环：

1. **咨询与房型**：检索宽捞候选 → 精排 → 模型基于资料与实时房态回答；知识库未命中时明确说明而不是编造。
2. **预订到入住**：订单子 Agent 创建订单并进入支付；支付回调确认后由 worker 触发入住流程，下发当日门锁凭证、房号与入住指引。
3. **客房服务**：送物请求创建机器人配送；清洁请求创建保洁派单并推送到保洁小程序。
4. **退房与改密**：退房触发退房清洁；保洁上传留证、确认改密后旧凭证立即吊销，任务收敛到 `closed`；超时未确认进入 `escalated` 并由管理人员处置。
5. **质检与运营**：`qa-auditor` 离线扫描会话与审计事实，产出质检标签、风险告警和数据质量信号；管理端以审计事实派生报表、评分卡与健康指标。

## 系统架构

```mermaid
flowchart TB
    subgraph Clients["三端"]
        G["客人小程序"]
        S["保洁小程序"]
        A["管理后台"]
    end

    subgraph Edge["接入层"]
        API["api 服务<br/>HTTP + SSE 流式"]
    end

    subgraph Runtime["Agent 运行时"]
        W["worker：AgentRun 执行"]
        TW["task-worker：后台任务 / 清洗管道"]
        MG["mcp-gateway<br/>lock / robot / wechat"]
    end

    subgraph Knowledge["知识库"]
        MV[("Milvus<br/>向量 + BM25")]
        N4[("Neo4j<br/>房型 / 周边关系图")]
    end

    subgraph Data["数据与存储"]
        PG[("PostgreSQL")]
        RD[("Redis")]
        MO[("MinIO / 数据湖")]
        DK["DuckDB<br/>OLAP 预聚合"]
    end

    subgraph Observe["可观测"]
        LF["Langfuse"]
    end

    G & S & A --> API
    API -->|"投递 AgentRun"| W
    W --> Knowledge
    W --> PG
    W --> LF
    TW --> MO --> DK
    W --> MG --> DS["device-sim 契约模拟器"]
    API --> PG & RD
```

| 服务 | 职责 |
| --- | --- |
| `api` | 三端 HTTP 接入、认证、SSE 推送 |
| `worker` | AgentRun 执行；密码轮换、任务收敛、质检跑批等后台任务 |
| `task-worker` | 数据清洗管道与数据湖导出（含 cron 调度） |
| `mcp-gateway` | 把门锁 / 机器人 / 微信渠道以 `streamable_http` MCP 暴露（`/lock/mcp`、`/robot/mcp`、`/wechat/mcp`） |
| `device-sim` | 门锁 / 机器人 / 微信支付的契约模拟器（含故障注入），真机联调前替代外部设备 |
| `guest-miniapp` / `staff-miniapp` | uni-app，构建微信小程序与 H5 调试 |
| `admin-web` | Vue 3 + Ant Design Vue 管理后台 |
| `postgres` / `redis` / `minio` | 业务事实、投递与事件、对象存储 |
| `milvus` / `neo4j` | 知识库向量、房型与周边关系图（compose 中归入 `knowledge` profile） |
| `bge-embed` / `bge-rerank` | 可选本地 embedding / rerank 服务（`knowledge` profile，默认不启用重排） |

存储分工：PostgreSQL 拥有业务事实、请求与 Run、审计与 LangGraph checkpoint；Redis 只承担投递、运行事件、取消信号与短期缓存；MinIO 拥有对象字节；Milvus 拥有向量坐标；Neo4j 拥有关系图。跨存储写按各自提交边界执行，失败显式标记并可从两侧查证。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph · LangChain · 中间件流水线 · Skills · supervisor 多智能体 |
| 后端 | FastAPI · ARQ · Alembic · SQLAlchemy (async) · Pydantic |
| 知识库 | Milvus（向量 + BM25 稀疏）· Neo4j · BGE-M3 embedding（可选）· Rerank |
| 数据 | PostgreSQL · Redis · MinIO · Parquet + zstd · DuckDB · PyArrow |
| 可观测 | Langfuse · `model_audit` / `tool_audit` / `route_audit` |
| 前端 | uni-app · Vue 3 · Vite · TypeScript · Ant Design Vue |
| 部署 | Docker Compose · nginx · GitHub Actions |

## 快速开始

前置：[Docker Desktop](https://www.docker.com/products/docker-desktop/)（Compose v2）。首次启动会构建 API / Worker 镜像并拉取 PostgreSQL、Redis、MinIO，建议预留足够磁盘与内存；知识库 profile 的 Milvus / Neo4j 镜像较大。

```bash
git clone https://github.com/haochenyaka/haochen.git
cd haochen

cp deploy/.env.example deploy/.env
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps

curl --fail http://localhost:8000/api/health
curl http://localhost:8000/api/system/ready

# 数据库迁移
docker compose -f deploy/docker-compose.yml exec api alembic upgrade head
```

需要知识库依赖时：

```bash
docker compose -f deploy/docker-compose.yml --profile knowledge up -d

# 本地 BGE-M3 embedding（可选，模型约 2.2 GiB）：先准备模型目录，再启动 bge-embed
python deploy/prepare_bge_models.py
docker compose -f deploy/docker-compose.yml --profile knowledge up -d bge-embed
# 按 deploy/.env.example 的注释启用 EMBEDDING_* 后重启 api/worker/task-worker，并全量重建索引
```

| 入口 | 地址 |
| --- | --- |
| API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/health |
| 就绪检查 | http://localhost:8000/api/system/ready |
| 设备模拟器 | http://localhost:9100/docs |
| MinIO 控制台 | http://localhost:9001 |
| 管理后台（开发态） | http://localhost:5175 |
| 客人 H5（开发态） | http://localhost:5173 |
| 保洁 H5（开发态） | http://localhost:5174 |

### 生产部署（反向代理 + TLS）

生产使用覆盖层：中间件与 API 不发布宿主端口，仅 nginx 暴露 80 / 443；启动时校验 `DATABASE_URL` / `NEO4J_PASSWORD` / `MINIO_SECRET_KEY` / `LOCK_MASTER_KEY` 等不再使用开发默认值，`CORS_ORIGINS` 不得为通配符，`DEV_IDENTITY_ENABLED` 必须关闭。

```bash
# 1. 在 deploy/.env 填齐生产必填项（见 .env.example 末段）
# 2. 将证书放到 deploy/nginx/certs/fullchain.pem 与 privkey.pem
# 3. 启动
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

X-Forwarded-For 由 nginx 覆盖写入，后端仅在 `RATE_LIMIT_TRUST_FORWARDED_FOR=true` 时采信。上线前检查见 [上线准备清单](docs/operations/launch-readiness-checklist.md)。

## 本地开发

### 后端

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash；Linux/macOS 用 source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy app
pytest
```

集成测试需要 PostgreSQL 与 Redis（`DATABASE_URL`、`REDIS_URL` 可覆盖 `backend/.env` 默认值）：

```bash
DATABASE_URL="postgresql+asyncpg://haochen:haochen@localhost:5432/haochen" pytest -q -m integration
```

### 种子数据与开发账号

```bash
alembic upgrade head
python -m app.seed                   # 可重复执行：按自然唯一键判存，不覆盖既有数据
python -m app.knowledge.seed         # 知识库样例内容：需 MinIO/Redis 可用，解析与索引任务需 worker 在跑
python -m app.knowledge.graph_seed   # 关系图：房型-设施 + 样例周边地点（需 Neo4j 可用）
```

种子会补齐 3 个房型、12 间客房（301–504）、2 名保洁员工与演示主链路（入住中订单 + 房间有效凭证 + 一条待接单呼叫清洁）。

| 用户名 | 角色 | 口令 |
| --- | --- | --- |
| `admin-demo` | admin | `admin-dev-pass` |
| `manager-demo` | manager | `manager-dev-pass` |
| `operator-demo` | operator | `operator-dev-pass` |
| `staff-2001` / `staff-2002` | staff | `staff-dev-pass` |
| `guest-1001` | customer | `guest-dev-pass` |

> 上表口令仅用于本地环境，生产必须重新签发。

登录换取 Bearer 令牌：

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin-demo","password":"admin-dev-pass"}'
```

### 前端

三端前端均为独立 npm 工程，Node 20+（CI 使用 Node 22）。开发态接口经 Vite 代理 `/api` 转发到 `http://localhost:8000`。

```bash
cd apps/guest-miniapp   # 客人小程序：H5 调试 5173 / 微信小程序 npm run build:mp-weixin
npm install && npm run dev:h5

cd apps/staff-miniapp   # 保洁小程序：H5 调试 5174 / 微信小程序 npm run build:mp-weixin
npm install && npm run dev:h5

cd apps/admin-web       # 管理后台：5175
npm install && npm run dev
```

每个工程的统一校验：`npm run lint && npm run type-check && npm run build`。

## 测试与 CI

CI 在 push `main` 与所有 PR 上运行 5 组作业：

| 作业 | 内容 |
| --- | --- |
| `backend` | `ruff check` · `ruff format --check` · `mypy app` · 单元测试（`pytest -m "not integration"`） |
| `integration` | PostgreSQL + Redis 服务容器；Alembic `upgrade → downgrade → upgrade` 回环；集成测试 |
| `backup-restore` | 真实执行加密备份、异地副本、保留期清理与丢表后恢复到新库 |
| `compose-config` | 校验开发 compose、`knowledge` profile 与生产覆盖层 |
| `frontend` | 三端 `npm ci → lint → type-check → build`；小程序额外构建 `mp-weixin` |

本地执行完整校验：

```bash
cd backend
ruff check . && ruff format --check . && mypy app
pytest -q -m "not integration"
pytest -q -m integration
```

## 评估基线

### 知识库评估

评估集为 JSONL（`backend/app/eval/kb_faq.jsonl`，169 题，金标为 `(document_title, ordinal)` 分块级锚点），CLI 逐题读当前检索配置出分，报告落 `kb_eval_reports` 表（只追加；内容 / 配置指纹变化时旧报告标记作废）：

```bash
cd backend
python -m app.eval.kb_runner --k 10 --out ../docs/eval/kb-baseline.json   # 加 --no-persist 只试跑不落库
```

已交付基线见 [docs/eval/kb-baseline.json](docs/eval/kb-baseline.json)：K=10、`chunk_recall@10` **0.9941**、`primary@1` **0.6746**，文档级 `recall@10` 0.9911 作为参考口径保留。无答案评判时 `overall_score` 以 `chunk_recall@10` 为主指标。该基线跑在本地 BGE-M3（`bge-m3:1024`）嵌入 + 词面重排上，覆盖 20 篇文档 / 95 个分块；旧的开发态确定性 embedding 基线（`chunk_recall@10` 0.9675、`primary@1` 0.6036）已由报告 ID 4 作废。

本地 BGE-M3 embedding 已接入（`EMBEDDING_BASE_URL`，TEI `/v1/embeddings`），查询延迟 p50 0.122s；真实分块 rerank 在本机 CPU 上 23 个候选需 53–59s，因此 `HttpReranker` 默认不启用，重排仍走词面 / LLM 链路。容量数据与接入细节见 [BGE spike 报告](docs/spikes/2026-10-17-bge-feasibility.md)。

### Agent 评估

评估集为 7 份 YAML（`backend/app/eval/agent/datasets/`，共 61 题），覆盖咨询、订房、入住、派单、多轮指代、转人工与边界负例。评测走生产同源链路（线程 / 消息 / 请求 → 队头 Run → supervisor 图），工具序列从 `tool_audit` 事实回读，报告落 `agent_eval_reports` 表（只追加；同数据集指纹下的旧报告在配置变化时标记作废）：

```bash
cd backend
pytest -m eval tests/integration/test_eval_agent_real.py
```

真实模型 `deepseek-chat` 下的已交付基线见 [docs/eval/agent-baseline.json](docs/eval/agent-baseline.json)：61/61 通过、平均工具准确率 1.0、参数匹配率 1.0、任务成功率 1.0，对应报告 ID 39–45（数据集 / 配置指纹与逐题工具序列见基线文件）。

答案质量由可选的 LLM-as-judge 补充：按相关性、事实性、语气、边界遵守四维给 0–1 分并附理由；同一答案自一致性采样 3 次取逐维中位数，评判温度 0.7（生成侧默认 0.2），配置指纹包含评判器身份。评判结果写入报告的 `avg_judge_score` / `judge_pass_rate` / `judge_dimensions` 与逐题 `judge` 字段。同一次全量评测中评判覆盖 61/61，四维均分 0.9622、单题通过率 0.9016（逐维均分：相关性 0.9344 / 事实性 0.9443 / 语气 0.9791 / 边界 0.9910）。评判器用 20 条人工标注样本校准，命令与产物：

```bash
cd backend
MODEL_API_KEY=... python -m app.eval.agent.calibration
```

已交付校准结果见 [docs/eval/agent-judge-calibration.json](docs/eval/agent-judge-calibration.json)：20/20 有效，判通过 / 不通过与人工标注完全一致，Cohen's kappa = **1.0**（逐维一致率 0.75–1.0，混淆计数与逐题明细见产物）。

前置条件：开发库已迁移并执行 `python -m app.seed`、知识库已导入（`query_kb` 用例）、设备模拟器在线（门锁密码用例），并配置 `MODEL_API_KEY` / `MODEL_NAME` / `MODEL_BASE_URL`。评测会为 `eval-*` 客人写入订单 / 任务等业务副作用；执行器在每条用例前释放旧评测订单，可重复执行。模型温度沿用 Agent 运行时配置（默认 0.2），跨轮组合类用例按单轮单意图设计以消除路由摇摆。

## 安全与合规

- **认证与授权**：登录签发不透明会话令牌（Redis 存储、滑动过期、逐请求复核账号状态）；角色守卫在路由依赖，资源归属在 service / repository 判定；拒绝语义分层为未认证 / 凭证失效 401、已认证但无权限 403、不可见资源 404。负向取证见 [P2 越权取证记录](docs/drills/p2-authz-drill.md)。
- **凭证安全**：门锁密码明文不落数据库、不进入消息历史、记忆与知识库；审计与日志导出对密码字段打码；`CredentialGuard` 在持久化边界统一处理。
- **副作用防护**：订单与门锁类工具注入幂等键，`ToolGuard` 拦截重复下单 / 重复改密；MCP 工具默认走审批，只读工具豁免。
- **生产部署**：覆盖层校验弱默认口令、CORS 通配符与开发身份开关；仅 nginx 暴露端口；`X-Forwarded-For` 仅在显式信任时采信。
- **运维与合规**：备份加密 + 异地副本 + 恢复演练；隐私说明、SLO 与值班、上线检查见 [运维文档](docs/operations/)。

## 目录结构

```
haochen/
├── backend/               # api / worker / task-worker / device-sim（FastAPI + ARQ）
│   ├── app/agents/        # supervisor、内置 Agent、中间件、Skills、MCP 运行时
│   ├── app/knowledge/     # 解析 / 分块 / 混合检索 / Rerank / 图谱
│   ├── app/data_pipeline/ # 清洗管道与数据湖
│   ├── app/eval/          # 知识库与 Agent 评估
│   ├── app/services/      # 订单、支付、门锁、清洁、报表等领域服务
│   ├── app/routers/       # 三端与管理端 API
│   └── app/worker/        # ARQ worker 与后台任务
├── apps/                  # guest-miniapp / staff-miniapp / admin-web
├── deploy/                # docker compose、env 模板、nginx、备份恢复脚本
├── docs/                  # 设计、开发流程、接口契约、运维、演练、评估基线
└── .github/workflows/     # CI（lint / type-check / 单测 / 集成测试 / 备份恢复 / 前端构建）
```

## 文档

- [核心服务端设计](docs/design.md)：三端能力、领域模型、关键链路与安全边界
- [多智能体运行时拓扑](docs/agent-topology.md)：supervisor + 4 子 Agent 的图、派发语义与事实归属
- [开发流程](docs/development-plan.md)：P0–P9 阶段划分、交付物与退出标准
- [机制基线与取舍](docs/mechanism-baseline.md)：分层边界、工具系统、MCP、知识库、评估等机制口径
- [验收主张取证矩阵](docs/acceptance-matrix.md)：A1–A10 的正例、负例与可复跑证据
- [剩余工作计划](docs/remaining-work-plan.md)：改造计划的阶段划分与历史记录
- [外部系统接口契约 v0](docs/contracts/external-systems-v0.md)：门锁 / 机器人 / 微信的接口假设、失败语义与预研结论
- [领域术语表](docs/glossary.md)：订单 / 凭证 / 派单状态机与运行时词汇的冻结口径
- [运维文档](docs/operations/)：备份恢复、上线准备、隐私合规、SLO 与值班
- 评估基线：[知识库](docs/eval/kb-baseline.json) · [Agent](docs/eval/agent-baseline.json) · [评判器校准](docs/eval/agent-judge-calibration.json) · [数据湖基准](docs/eval/datalake-benchmark.md)

## 说明

- 知识库样例为占位内容（酒店介绍、WiFi / 设备、早餐、入住退房政策、客房服务、FAQ），标题带【样例】前缀，上线前须替换为真实资料；样例文件在 `backend/app/knowledge/samples/`。
- 关系图的周边地点为样例数据（虚构距离与步行时间），上线前需替换为门店实测数据。
- 图补充腿使用确定性关键词 / 正则抽取（设施、周边类别、距离、步行时间），未登记的同义改写不会触发；未命中或图不可用时退回纯知识库检索。
- PMS 房态同步尚未落地；门锁、机器人与微信支付通过 `device-sim` 契约模拟器联调，真机接入前需按 [外部系统接口契约](docs/contracts/external-systems-v0.md) 完成适配。
- 评估数字来自仓库内已交付的基线与本机环境（本地 BGE-M3 / `deepseek-chat`），用于回归对照，不代表生产环境的绝对指标。

## License

[MIT](./LICENSE)
