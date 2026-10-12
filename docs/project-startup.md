# HaoChen 智能酒店 Agent — 项目启动文档

> 版本：1.0 | 更新日期：2026-09-30 | 状态：定稿

---

## 1. 项目概览

皓晨酒店无人 Agent 系统：一套由三个内置 Agent（前台接待 front-desk、员工助手 housekeeping-assistant、质检员 qa-auditor）支撑的三端智能体系统，覆盖客人入住全流程、保洁作业与管理后台。

| 端 | 载体 | 核心能力 |
|---|------|----------|
| C 端客人 | 微信小程序 | 咨询、预订、支付、客房服务、退房、投诉 |
| 保洁员工 | 微信小程序 | 任务接收、退房留证、上下班打卡审核 |
| 管理后台 | Web (admin-web) | Agent 配置、会话质检、报表日志、基础数据、记忆管理、人工接线 |

### 开发阶段总览

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 立项与基建（Compose / CI / 三层骨架 / 审计表） | ✅ 完成 |
| P1 | 运行时骨架（接入/FIFO/租约/中间件/SSE/审计） | ✅ 完成 |
| P2 | 领域模型、基础数据与授权基座 | ✅ 完成 |
| P3 | 知识库与咨询链路 | ✅ 完成 |
| P4 | 订单与门锁凭证 | ✅ 完成 |
| P5 | 客房服务、机器人配送与保洁派单 | ✅ 完成 |
| P6 | 打卡与视觉审核 | ✅ 完成 |
| **P7** | **管理后台全模块** | **⏳ 进行中（7.4/7.5/7.9 已交付）** |
| P8 | 评估闭环与数据质量 | ⬜ 待启动 |
| P9 | 硬件联调、安全加固与上线 | ⬜ 待启动 |

---

## 2. 技术栈

### 后端

| 组件 | 选型 | 用途 |
|------|------|------|
| Web 框架 | FastAPI >=0.115 | HTTP 接入、SSE 推送 |
| 运行时 | LangGraph >=1.0 | Agent 图装配、checkpoint |
| 队列 | ARQ >=0.26 | AgentRun 执行 + Durable Task |
| 主库 | PostgreSQL 16（asyncpg） | 业务事实、审计事实（唯一权威） |
| 缓存/队列后端 | Redis 7（append-only） | 投递队列、租约、SSE 事件总线 |
| 对象存储 | MinIO | 知识库原件、留证照片/视频 |
| 向量库 | Milvus 2.5（BM25 + 向量混合搜索） | 知识库检索 |
| 图库 | Neo4j 5 | 房型-设施-周边关系图 |
| 迁移 | Alembic | 数据库版本管理 |

### 前端

| 端 | 栈 | 端口 |
|-----|-----|------|
| admin-web | Vue 3 + Ant Design Vue 4 + TypeScript + Vite | 5175 |
| guest-miniapp | uni-app（Vue 3）→ 微信小程序 / H5 | 5173 |
| staff-miniapp | uni-app（Vue 3）→ 微信小程序 / H5 | 5174 |

### 基础设施

Docker Compose 管理全部服务依赖；CI 含 lint/type-check/test/migration/secret-scan。

---

## 3. 项目结构

```
haochen/
├── docs/                          # 设计文档、开发流程、决策记录
│   ├── design.md                  # 核心服务端设计
│   ├── development-plan.md        # P0–P9 阶段划分与退出标准
│   ├── project-startup.md         # 本文件：启动指引
│   ├── glossary.md                # 领域术语表
│   ├── mechanism-baseline.md      # 平台机制基线
│   └── 2026-09-27-hotel-agent-server-design.md  # 架构选型记录
├── backend/                       # FastAPI 服务端（api + worker + device-sim）
│   ├── app/
│   │   ├── main.py                # 应用工厂
│   │   ├── config.py              # pydantic-settings 配置
│   │   ├── db.py                  # async SQLAlchemy 引擎
│   │   ├── routers/               # 路由层（认证 + 输入校验）
│   │   ├── services/              # 用例层（订单/派单/报表/日志）
│   │   ├── repositories/          # 持久化层（可见性查询/幂等键）
│   │   ├── agents/                # Agent 图装配、中间件、工具注册
│   │   │   ├── middlewares/       # 11 道中间件工位
│   │   │   ├── toolkits/          # 工具注册表（@builtin_tool）
│   │   │   ├── skills/            # 技能（checkin-flow 等）
│   │   │   └── buildin/           # 内置 Agent 定义
│   │   ├── worker/                # ARQ 入口（AgentRun + 后台任务）
│   │   ├── models/                # SQLAlchemy 模型
│   │   ├── device_sim/            # 门锁/机器人/微信 契约模拟器
│   │   ├── domain/                # 领域逻辑（状态机等）
│   │   ├── knowledge/             # 知识库管理器 + Milvus 实现
│   │   ├── security/              # 认证与授权
│   │   ├── vision_api/            # 人脸/工牌/工服检测
│   │   ├── lock/                  # 密码管理
│   │   ├── wechat/                # 微信集成
│   │   └── eval/                  # 知识库评估套件
│   ├── tests/                     # unit / integration / e2e / security
│   ├── migrations/                # Alembic 迁移
│   └── pyproject.toml
├── apps/
│   ├── admin-web/                 # 管理后台（Vite 5175）
│   ├── guest-miniapp/             # 客人小程序（uni-app, H5 5173）
│   └── staff-miniapp/             # 保洁小程序（uni-app, H5 5174）
├── deploy/
│   ├── docker-compose.yml         # 全服务编排
│   └── .env.example               # 环境变量占位模板
└── .github/workflows/             # CI 流水线
```

---

## 4. 前置条件

| 依赖 | 版本要求 | 备注 |
|------|----------|------|
| Docker Desktop | Compose v2+ | 启动全部后端依赖服务 |
| Python | >=3.12 | 后端开发 |
| Node.js | >=20 | 前端开发 |
| Git Bash / WSL2 | — | Windows 推荐 shell |

---

## 5. 快速启动（Docker Compose 全栈）

### 5.1 启动基础设施 + API + Worker

```bash
# 1. 克隆并进入项目
cd haochen

# 2. 配置环境变量（从模板复制，开发态默认值即可运行）
cp deploy/.env.example deploy/.env

# 3. 启动所有服务（不含知识库依赖）
docker compose -f deploy/docker-compose.yml up -d --build

# 4. 验证服务状态
docker compose -f deploy/docker-compose.yml ps

# 5. 检查 API 健康状况
curl --fail http://localhost:8000/api/health

# 6. 执行数据库迁移
docker compose -f deploy/docker-compose.yml exec api alembic upgrade head

# 7. 可选：启动知识库依赖（Milvus + Neo4j，镜像较大）
docker compose -f deploy/docker-compose.yml --profile knowledge up -d
```

### 5.2 可用端点

| 服务 | 地址 | 说明 |
|------|------|------|
| API (FastAPI docs) | http://localhost:8000/docs | Swagger UI |
| API (健康检查) | http://localhost:8000/api/health | Health check |
| API (就绪检查) | http://localhost:8000/api/system/ready | Readiness probe |
| 设备模拟器 | http://localhost:9100/docs | 门锁/机器人/微信 mock |
| MinIO 控制台 | http://localhost:9001 | 对象存储管理 |
| Milvus | localhost:19530 | 向量库 |
| Neo4j 浏览器 | http://localhost:7474 | 图数据库管理 |

### 5.3 种子数据

```bash
# 基础数据（房型/房间/员工/演示订单/凭证/任务）
docker compose -f deploy/docker-compose.yml exec api python -m app.seed

# 知识库样例内容（需 MinIO + Worker 运行中）
docker compose -f deploy/docker-compose.yml exec api python -m app.knowledge.seed

# 关系图种子（需 Neo4j 可用）
docker compose -f deploy/docker-compose.yml exec api python -m app.knowledge.graph_seed
```

种子数据包含：3 个房型、12 间客房（301–504）、2 名保洁员工、演示入住中订单及待接单呼叫清洁任务。

### 5.4 开发账号

| 用户名 | 角色 | 口令 | 登录获取 Token |
|--------|------|------|----------------|
| `admin-demo` | admin | `admin-dev-pass` | `curl -X POST http://localhost:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin-demo","password":"admin-dev-pass"}'` |
| `manager-demo` | manager | `manager-dev-pass` | 同上 |
| `operator-demo` | operator | `operator-dev-pass` | 同上 |
| `staff-2001` / `staff-2002` | staff | `staff-dev-pass` | 同上 |
| `guest-1001` | customer | `guest-dev-pass` | 同上 |

> **开发账号口令仅限本地环境**，生产必须重新签发并更换密钥。

---

## 6. 本地开发（后端）

### 6.1 虚拟环境与依赖

```bash
cd backend
python -m venv .venv
# Windows (Git Bash)
source .venv/Scripts/activate
# Linux/macOS
# source .venv/bin/activate
pip install -e ".[dev]"
```

### 6.2 代码质量检查

```bash
ruff check . && ruff format --check . && mypy app
```

### 6.3 运行测试

```bash
# 全部测试
pytest

# 仅单元测试
pytest -q -m "not integration"

# 集成测试（需要 PostgreSQL + Redis 运行中）
DATABASE_URL="postgresql+asyncpg://haochen:haochen@localhost:5432/haochen" pytest -q -m integration
```

### 6.4 常用开发命令

```bash
# API 服务（热重载，默认 8000）
uvicorn app.main:app --reload

# 设备模拟器（热重载，9100）
uvicorn app.device_sim.main:app --reload --port 9100

# Worker（需要 Redis）
arq --custom-log-dict app.worker.settings.WORKER_LOG_CONFIG app.worker.settings.WorkerSettings

# 后台任务 Worker（独立队列）
arq --custom-log-dict app.worker.settings.WORKER_LOG_CONFIG app.worker.settings.TaskWorkerSettings

# 数据库迁移
alembic upgrade head
alembic revision -m "描述"

# 知识库评估基线
python -m app.eval.kb_runner --k 10 --out ../docs/eval/kb-baseline.json
```

### 6.5 模型配置

后端支持 OpenAI 兼容接口，通过环境变量配置：

```bash
# compose 中 MODEL_API_KEY 留空时自动回退系统环境变量 DEEP_SEEK_API_KEY
# 两者都为空时降级为开发态 stub 模型（回复带 [dev-stub] 前缀，无需网络）
export DEEP_SEEK_API_KEY=sk-xxx
```

---

## 7. 本地开发（前端）

三端均为独立 npm 工程。开发态接口经 Vite 代理 `/api` 转发到 `http://localhost:8000`（需先启动后端或 docker compose 栈）。

```bash
# 管理后台（5175）
cd apps/admin-web
npm install
npm run dev

# 客人小程序 H5 调试（5173）
cd apps/guest-miniapp
npm install
npm run dev:h5

# 保洁小程序 H5 调试（5174）
cd apps/staff-miniapp
npm install
npm run dev:h5
```

每个工程的统一校验：

```bash
npm run lint && npm run type-check && npm run build
```

---

## 8. 认证与授权

### 8.1 开发态身份注入

`DEV_IDENTITY_ENABLED=true` 时，可通过 `X-Dev-Username` 请求头直接注入身份（免登录调试）：

```bash
curl http://localhost:8000/api/admin/reports/overview \
  -H 'X-Dev-Username: admin-demo'
```

### 8.2 生产授权模型

| 角色 | 访问范围 |
|------|----------|
| admin | 全部读写 |
| manager | 报表/日志/会话只读 |
| staff | 仅本人任务载荷 |
| customer | 仅本人与本人订单 |

授权边界在 FastAPI 依赖 + repository 可见性查询；前端守卫只影响体验，不构成授权。

---

## 9. Docker Compose 服务参考

| 容器 | 功能 | 健康检查 | 依赖 |
|------|------|----------|------|
| postgres | 业务 + 审计数据库 | pg_isready | — |
| redis | 队列 + 缓存 + SSE 总线 | redis-cli ping | — |
| minio | 对象存储 | /minio/health/live | — |
| milvus（knowledge profile） | 向量 + BM25 混合检索 | /healthz | — |
| neo4j（knowledge profile） | 关系图 | cypher-shell | — |
| device-sim | 门锁/机器人/微信 mock | /health | — |
| api | FastAPI 接入 | /api/health | postgres, redis, device-sim |
| worker | AgentRun 执行器 | arq --check | postgres, redis, device-sim |
| task-worker | 后台任务（密码轮换/知识索引） | arq --check | postgres, redis |

知识库相关服务（milvus, neo4j）需 `--profile knowledge` 参数启动。

---

## 10. 测试策略

| 层级 | 范围 | 运行方式 |
|------|------|----------|
| 单元测试 | 状态机、幂等、授权判定、打码函数 | `pytest -m "not integration"` |
| 集成测试 | 接入链、租约恢复、幂等回调、报表派生 | `pytest -m integration`（需真实 PG/Redis） |
| E2E 测试 | 支付→入住→清洁→退房全链路 | `pytest -m e2e`（需 compose 全栈） |
| 安全测试 | 越权矩阵、明文扫描 | `pytest -m security` |

迁移测试（Alembic upgrade/downgrade 双向校验）在 CI 中执行。

---

## 11. P7 剩余步骤（待交付模块）

当前 P7 已完成 7.4（数据报表）、7.5（智能体日志）、7.9（运行健康面板），剩余 6 个模块：

| 模块 | 内容 | 依赖 |
|------|------|------|
| **7.1 Agent 配置** | 提示词配置（草稿/发布/版本回滚）、知识库上传、工具权限管理、工作流编排 | P7.2 模型配置完成后联动 |
| **7.2 Agent 管理** | Skills 管理（渐进加载/启停/版本）、MCP 管理（启用/配置/连通测试）、模型自定义配置（供应商/密钥/参数/限额） | 独立 |
| **7.3 会话监控** | 全量对话检索、qa-auditor 自动打标（解决/未解决/转人工/投诉风险/幻觉嫌疑）、风险告警实时推送 | qa-auditor Agent 已有基座 |
| **7.6 酒店基础数据页面** | 房型/价格/房态/员工账号/权限的管理界面（复用 P2 API） | P2 API 已就位 |
| **7.7 记忆管理** | 查看/清理客人记忆、敏感信息过滤、删除级联清理向量副本 | — |
| **7.8 人工工作台** | 转人工请求队列、店长接单与回复、转人工率回流报表 | P7.3 提供入口 |

### P7 之后

| 阶段 | 内容 |
|------|------|
| **P8** | 智能体评估（Langfuse 驱动）、知识库评估回归、数据质量评分卡、在线反馈接入 |
| **P9** | 真实门锁/机器人联调、安全加固与混沌演练、灰度上线（单店→全店）、6 条验收主张全部取证 |

---

## 12. 常见问题

**Q: docker compose 启动后 API 不健康？**
先确认 postgres 和 redis 的 healthcheck 通过（`docker compose ps`），然后运行 `alembic upgrade head`。

**Q: 集成测试连不上数据库？**
检查 `DATABASE_URL` 环境变量，默认 `localhost:5432`；Docker compose 启动后 PG 映射到主机 5432 端口。

**Q: Worker 启动后没有消费任务？**
确认 `REDIS_URL` 正确且 Redis 可达；worker 日志中应有 "WorkerSettings" 启动日志。

**Q: Milvus 启动慢或启动失败？**
Milvus 首次启动需要拉取 embedding 模型和创建索引，属于正常现象；检查 `docker compose logs milvus` 确认进度。

**Q: 前端请求后端跨域？**
Vite 开发服务器已配置 `/api` 代理到 `http://localhost:8000`，确保先启动后端。

---

## 13. 参考文档

| 文档 | 说明 |
|------|------|
| `docs/design.md` | 核心服务端设计（领域模型、关键链路、安全边界） |
| `docs/development-plan.md` | P0–P9 完整开发流程、交付物、退出标准 |
| `docs/glossary.md` | 领域术语表（订单/凭证/派单状态机冻结口径） |
| `docs/mechanism-baseline.md` | 平台机制选型与取舍决策 |
| `docs/2026-09-27-hotel-agent-server-design.md` | 架构选型决策记录 |
| `docs/contracts/external-systems-v0.md` | 外部系统接口契约 v0 |
| `backend/README.md` | 后端本地开发简版 |
| `README.md` | 项目根 README（快速入门） |