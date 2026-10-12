# haochen-server

后端服务代码：api（HTTP 接入）、worker（后台任务）、device-sim（外部设备契约模拟器）。

## 本地开发

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash；Linux/macOS 用 source .venv/bin/activate
pip install -e ".[dev]"

ruff check . && ruff format --check . && mypy app
pytest
```

## 常用命令

```bash
# api（默认 8000）
uvicorn app.main:app --reload

# 设备模拟器（默认 9100）
uvicorn app.device_sim.main:app --reload --port 9100

# worker（需要 Redis）
arq --custom-log-dict app.worker.settings.WORKER_LOG_CONFIG app.worker.settings.WorkerSettings

# 数据库迁移（需要 PostgreSQL）
alembic upgrade head
alembic revision -m "描述"
```

## 配置

配置项见 `app/config.py`，通过环境变量或 `.env` 注入；本地默认值指向 `localhost`。compose 环境下的取值见 `deploy/docker-compose.yml`，占位清单见 `deploy/.env.example`。

模型接入走 OpenAI 兼容接口（`MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_TEMPERATURE`）；compose 将留空的 `MODEL_API_KEY` 回退直通系统环境变量 `DEEP_SEEK_API_KEY`，无需把密钥写进任何文件；两者都为空时后端自动降级为开发态 stub 模型（无网络、回复带 `[dev-stub]` 前缀），本地与测试无需外部模型即可跑通全链路。

中间件流水线（11 道，顺序见 docs/mechanism-baseline.md §3）在 `app/agents/middlewares/`：上下文压力阈值与卸载阈值只由环境变量控制（`SUMMARY_THRESHOLD` / `SUMMARY_KEEP_MESSAGES` / `WORKDIR_ROOT` / `LARGE_RESULT_THRESHOLD_BYTES` / `LARGE_RESULT_PREVIEW_CHARS` / `MODEL_RETRY_MAX_ATTEMPTS` / `MODEL_RETRY_BACKOFF_SECONDS`）；Agent 级差异通过配置 `middleware` 段做工位字段覆盖，未知工位或字段直接报错。大工具结果与摘要历史落 `WORKDIR_ROOT/threads/<thread_id>/outputs/`（默认 `data/workdir`）。

工具走 `app/agents/toolkits/`：`@builtin_tool(category=...)` 注册进注册表，注册元数据（skill / dangerous / read_only / display_name）同时用于 Skill 门控、审批判定与（P7）管理端展示；工具意图由 Agent 的 `tool_ids` 声明，经资源归一化授权后进入模型请求与 ToolNode，执行处写 tool_audit 事实。P1 只读演示工具 `get_current_time` 已挂在 front-desk；写类与危险工具随 P3–P5 逐个落地。

运行事件流（SSE）在 `app/services/`：`run_event_bus.py`（Redis Stream 总线，按 Run 隔离并带 TTL 与长度上限；Redis 不可达降级为空实现，不阻断进程启动）、`run_events.py`（事件协议与每 Run 发射器：run.started、message.delta、tool.call、tool.result、message.completed、run.completed、run.failed、run.cancelled、handoff.requested、interrupt）、`run_stream.py`（回放 + 阻塞跟随 + 数据库终态兜底）。`GET /api/guest/chat/stream?run_id=` 消费该流：事件 id 即传输游标，标准 `Last-Event-ID` 头或 `last_event_id` 查询参数用于断线续传；事件是投影、PostgreSQL 是事实来源，事件缺失（Redis 降级、租约收敛、订阅晚于结束）时按数据库合成终态；配置项 `RUN_EVENT_TTL_SECONDS` / `RUN_EVENT_MAXLEN` / `SSE_HEARTBEAT_SECONDS` / `SSE_FOLLOW_BLOCK_MS`。

恢复演练在 `tests/integration/test_recovery_drill.py`：杀 worker 收敛、重复投递恰好一次、重启补发、Redis 抖动降级、明文扫描骨架与 M1 全链路验收；记录见 `docs/drills/p1-recovery-drill.md`。运行方式：`DATABASE_URL=... pytest tests/integration/test_recovery_drill.py`。

## 目录说明

```
app/
├── main.py          # FastAPI 应用工厂
├── config.py        # 配置（pydantic-settings）
├── db.py            # async SQLAlchemy 引擎与会话
├── routers/         # 薄路由：认证与输入校验
├── services/        # 用例层
├── repositories/    # 持久化层
├── agents/          # Agent 图装配、中间件、工具注册（P1 起）
├── worker/          # ARQ worker 入口与任务注册（P1 起）
├── models/          # SQLAlchemy 模型（P0 基线：审计表）
└── device_sim/      # 门锁/机器人/微信支付契约模拟器
migrations/          # Alembic 迁移
tests/               # 单元与集成测试
```
