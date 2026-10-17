# HaoChen · 无人酒店多智能体系统

> 面向无人酒店的 AI Agent 全栈系统：微信小程序（客人 / 保洁）、Web 管理后台三端一体，由 LangGraph 编排的智能体驱动入住、客房服务与质检的全流程。

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0-1C3C3C)](https://langchain-ai.github.io/langgraph/)
[![Vue](https://img.shields.io/badge/Vue-3-42B883?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](./deploy/docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## 项目亮点

- **三端一体的 Agent 落地**：客人小程序、保洁小程序、管理后台共用一套 Agent 运行时，把自然语言对话接到真实的订单、门锁、客房服务业务链路上，而不是只做问答 Demo。
- **混合检索知识库**：Milvus 向量检索（含 BM25 稀疏向量）+ Neo4j 关系图补充腿（房型设施 AND、周边类别/距离多跳，故障自动降级），经 Rerank 精排管道；169 题分块级黄金锚点的 `chunk_recall@10` 达 **0.9941**、`primary@1` **0.6746**（本地 BGE-M3 embedding，详见 [评估基线](docs/eval/kb-baseline.json)）。
- **可观测的 Agent 运行时**：Langfuse 做数据集驱动的评估与链路追踪，全量审计事实可通过 SQL 聚合派生报表，端到端可观测。
- **数据湖分析层**：业务表增量导出到 MinIO（Parquet + zstd），由嵌入的 DuckDB 直读生成按日与业务预聚合（房型月度入住率/客单价）；20 万订单规模下同一业务查询中位耗时 **568ms → 1.5ms（约 378x）**，预聚合缺失时自动降级回 PostgreSQL 重算（[基准与口径](docs/eval/datalake-benchmark.md)）。
- **异步与隔离**：API 与 Worker 分离，LangGraph 会话执行与知识索引、质检跑批等后台任务解耦，避免长任务阻塞交互链路。

## 界面预览

> 截图已预留，放入 `docs/screenshots/` 后即可显示。

| 客人小程序 | 保洁小程序 | 管理后台 |
| --- | --- | --- |
| ![客人端](./docs/screenshots/guest.png) | ![保洁端](./docs/screenshots/staff.png) | ![管理后台](./docs/screenshots/admin.png) |
| 在线咨询 · 预订 · 客房服务 | 任务接收 · 打卡 · 退房留证 | Agent 配置 · 会话监控 · 报表 |

## 三端与核心能力

| 端 | 载体 | 核心能力 |
| --- | --- | --- |
| C 端客人 | 微信小程序（uni-app） | 酒店咨询、房型查询、预订/改期/取消/续订、发票申请、客房需求、退房、投诉、周边推荐 |
| 保洁员工 | 微信小程序（uni-app） | 任务接收与执行、退房清洁改密留证、上下班打卡审核 |
| 管理后台 | Web（Vue 3 + Ant Design Vue） | Agent 配置与管理、会话监控与质检、数据报表、智能体日志、酒店基础数据、记忆管理、人工接线 |

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
    API --> PG & RD
```

| 服务 | 职责 |
| --- | --- |
| `api` | 三端 HTTP 接入、认证、SSE 推送 |
| `worker` | AgentRun 执行；密码轮换、任务收敛、质检跑批等后台任务 |
| `task-worker` | 数据清洗管道与数据湖导出 |
| `device-sim` | 门锁 / 机器人 / 微信支付的契约模拟器（含故障注入），真机联调前替代外部设备 |
| `guest-miniapp` / `staff-miniapp` | uni-app，构建微信小程序与 H5 调试 |
| `admin-web` | Vue 3 + Ant Design Vue 管理后台 |
| `postgres` / `redis` / `minio` | 业务事实、投递与事件、对象存储 |
| `milvus` / `neo4j` | 知识库向量、房型与周边关系图（compose 中归入 `knowledge` profile） |

## 技术栈

| 层 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph · LangChain · 中间件流水线 · Skills |
| 后端 | FastAPI · ARQ · Alembic · SQLAlchemy (async) |
| 知识库 | Milvus（向量 + BM25 稀疏）· Neo4j · BGE-M3 embedding（可选）· Rerank |
| 数据 | PostgreSQL · Redis · MinIO · Parquet + zstd · DuckDB · PyArrow |
| 可观测 | Langfuse |
| 前端 | uni-app · Vue 3 · Vite · TypeScript · Ant Design Vue |
| 部署 | Docker Compose |

## 快速开始

前置：[Docker Desktop](https://www.docker.com/products/docker-desktop/)（Compose v2 起）。

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

# 需要知识库依赖时（镜像较大）
docker compose -f deploy/docker-compose.yml --profile knowledge up -d

# 本地 BGE-M3 embedding（可选，模型约 2.2 GiB）：先准备模型目录，再启动 bge-embed
python deploy/prepare_bge_models.py
docker compose -f deploy/docker-compose.yml --profile knowledge up -d bge-embed
# 按 deploy/.env.example 的注释启用 EMBEDDING_* 后重启 api/worker/task-worker，并全量重建索引
```

服务入口：API 文档 http://localhost:8000/docs 、设备模拟器 http://localhost:9100/docs 、MinIO 控制台 http://localhost:9001 。

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

三端前端均为独立 npm 工程，Node 20+。开发态接口经 Vite 代理 `/api` 转发到 `http://localhost:8000`。

```bash
cd apps/guest-miniapp   # 客人小程序：H5 调试 5173 / 微信小程序 npm run build:mp-weixin
npm install && npm run dev:h5

cd apps/staff-miniapp   # 保洁小程序：H5 调试 5174 / 微信小程序 npm run build:mp-weixin
npm install && npm run dev:h5

cd apps/admin-web       # 管理后台：5175
npm install && npm run dev
```

每个工程的统一校验：`npm run lint && npm run type-check && npm run build`。

## 知识库评估基线

评估集为 JSONL（`backend/app/eval/kb_faq.jsonl`，169 题，金标为 `(document_title, ordinal)` 分块级锚点），CLI 逐题读当前检索配置出分，报告落 `kb_eval_reports` 表（只追加；内容/配置指纹变化时旧报告标记作废）：

```bash
cd backend
python -m app.eval.kb_runner --k 10 --out ../docs/eval/kb-baseline.json   # 加 --no-persist 只试跑不落库
```

已交付基线见 [docs/eval/kb-baseline.json](docs/eval/kb-baseline.json)：K=10、`chunk_recall@10` **0.9941**、`primary@1` **0.6746**，文档级 `recall@10` 0.9911 作为参考口径保留。无答案评判时 `overall_score` 以 `chunk_recall@10` 为主指标。该基线跑在本地 BGE-M3（`bge-m3:1024`）嵌入 + 词面重排上，覆盖 20 篇文档 / 95 个分块；旧的开发态确定性 embedding 基线（`chunk_recall@10` 0.9675、`primary@1` 0.6036）已由报告 ID 4 作废。

本地 BGE-M3 embedding 已接入（`EMBEDDING_BASE_URL`，TEI `/v1/embeddings`），查询延迟 p50 0.122s；真实分块 rerank 在本机 CPU 上 23 个候选需 53–59s，因此 `HttpReranker` 默认不启用，重排仍走词面/LLM 链路。容量数据与接入细节见 [BGE spike 报告](docs/spikes/2026-10-17-bge-feasibility.md)。

## Agent 评估基线

评估集为 7 份 YAML（`backend/app/eval/agent/datasets/`，共 61 题），覆盖咨询、订房、入住、派单、多轮指代、转人工与边界负例。评测走生产同源链路（线程/消息/请求 → 队头 Run → supervisor 图），工具序列从 `tool_audit` 事实回读，报告落 `agent_eval_reports` 表（只追加；同数据集指纹下的旧报告在配置变化时标记作废）：

```bash
cd backend
pytest -m eval tests/integration/test_eval_agent_real.py
```

真实模型 `deepseek-chat` 下的已交付基线见 [docs/eval/agent-baseline.json](docs/eval/agent-baseline.json)：61/61 通过、平均工具准确率 1.0、参数匹配率 1.0、任务成功率 1.0，对应报告 ID 39–45（数据集/配置指纹与逐题工具序列见基线文件）。

答案质量由可选的 LLM-as-judge 补充：按相关性、事实性、语气、边界遵守四维给 0–1 分并附理由；同一答案自一致性采样 3 次取逐维中位数，评判温度 0.7（生成侧默认 0.2），配置指纹包含评判器身份。评判结果写入报告的 `avg_judge_score` / `judge_pass_rate` / `judge_dimensions` 与逐题 `judge` 字段。同一次全量评测中评判覆盖 61/61，四维均分 0.9622、单题通过率 0.9016（逐维均分：相关性 0.9344 / 事实性 0.9443 / 语气 0.9791 / 边界 0.9910）。评判器用 20 条人工标注样本校准，命令与产物：

```bash
cd backend
MODEL_API_KEY=... python -m app.eval.agent.calibration
```

已交付校准结果见 [docs/eval/agent-judge-calibration.json](docs/eval/agent-judge-calibration.json)：20/20 有效，判通过/不通过与人工标注完全一致，Cohen's kappa = **1.0**（逐维一致率 0.75–1.0，混淆计数与逐题明细见产物）。

前置条件：开发库已迁移并执行 `python -m app.seed`、知识库已导入（`query_kb` 用例）、设备模拟器在线（门锁密码用例），并配置 `MODEL_API_KEY` / `MODEL_NAME` / `MODEL_BASE_URL`。评测会为 `eval-*` 客人写入订单/任务等业务副作用；执行器在每条用例前释放旧评测订单，可重复执行。模型温度沿用 Agent 运行时配置（默认 0.2），跨轮组合类用例按单轮单意图设计以消除路由摇摆。

## 目录结构

```
haochen/
├── backend/               # api / worker / task-worker / device-sim（FastAPI + ARQ）
│   ├── app/agents/        # Agent 运行时：注册表、中间件流水线、工具集
│   ├── app/knowledge/     # 解析 / 分块 / 混合检索 / Rerank / 图谱
│   ├── app/data_pipeline/ # 清洗管道与数据湖
│   └── app/eval/          # 知识库与 Agent 评估
├── apps/                  # guest-miniapp / staff-miniapp / admin-web
├── deploy/                # docker compose、env 模板、备份恢复脚本
├── docs/                  # 设计、开发流程、接口契约、演练记录
└── .github/workflows/     # CI（lint / type-check / 单测 / 集成测试）
```

## 文档

- [核心服务端设计](docs/design.md)：三端能力、领域模型、关键链路与安全边界
- [开发流程](docs/development-plan.md)：P0–P9 阶段划分、交付物与退出标准
- [外部系统接口契约 v0](docs/contracts/external-systems-v0.md)：门锁 / 机器人 / 微信的接口假设、失败语义与预研结论
- [领域术语表](docs/glossary.md)：订单 / 凭证 / 派单状态机与运行时词汇的冻结口径

## 说明

- 知识库样例为占位内容（酒店介绍、WiFi/设备、早餐、入住退房政策、客房服务、FAQ），标题带【样例】前缀，上线前须替换为真实资料；样例文件在 `backend/app/knowledge/samples/`。
- 关系图的周边地点为样例数据（虚构距离与步行时间），上线前需替换为门店实测数据。
- 图补充腿使用确定性关键词/正则抽取（设施、周边类别、距离、步行时间），未登记的同义改写不会触发；未命中或图不可用时退回纯知识库检索。

## License

[MIT](./LICENSE)