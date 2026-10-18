# MCP 服务器「从配置壳到运行时真接」PR 级实施步骤

状态：待评审（draft）
日期：2026-10-18
上游：[remediation-plan.md](./remediation-plan.md) · [mechanism-baseline.md](./mechanism-baseline.md) §2.7 · [design.md](./design.md) §Agent 运行时设计
范围：PR-M1 MCP 客户端与连接测试 · PR-M2 运行期接线（MCP 工具注入 + 授权快照） · PR-M3 mcp_gateway 独立服务 · PR-M4 验收与文档

> 用户已拍板：**全链路真接**；客户端用**官方 `mcp` SDK + `langchain-mcp-adapters`**。

---

## 0. 本轮对上游/文档的修正（细读代码后）

| 项 | 文档/直觉判断 | 细读后的修正 | 影响 |
| --- | --- | --- | --- |
| 现状 | 「MCP 服务器管理已交付」 | 只交付了**配置表 + CRUD + 管理页**；**运行时零接线**——`McpServer` 表在 `backend/app/` 内除 router 外无任何消费者 | 「补实现」的重心在运行时，不在 CRUD |
| 连接测试 | `admin_mcp_router.py:1` docstring 写「CRUD 与连接测试」 | **没有任何连接测试端点**，`McpServerService` 无连接方法 | 需新建 MCP 客户端与探测链路（PR-M1） |
| 传输层 | `mechanism-baseline.md:101` 称「只接受 streamable_http/sse、工具对象按配置哈希缓存、连接测试只发现工具」 | 代码中**一条都不存在**；`pyproject.toml` 无任何 MCP 依赖 | 文档是**目标态**而非现状，本轮将其落地 |
| gateway | `design.md:26`、`mechanism-baseline.md:103` 称落地于 `app/mcp_gateway/`（lock/robot/wechat/pms 各一个 MCP） | **该目录不存在** | 需新建（PR-M3），否则运行时接线无本地可自证对象 |
| 资源快照 | `context.py:118,215` 已建模 `mcp_servers` 桶 | 请求侧从不构造 mcp ref（`run_body.py:156-169`）、策略侧对 `mcp_server` 直接 `return False`（`context.py:179`） | 三处断点，PR-M2 逐一接通 |
| 验收 | — | `acceptance-matrix.md` / `remaining-work-plan.md` **零 MCP 条目** | 说明 MCP 从未进入验收面；PR-M4 补 A9 |

**核心断点（file:line）**
1. `backend/app/services/run_body.py:156-169` — `AgentResourceRequest` 只构造 `tool`/`knowledge_base`/`skill`，**无 `mcp_server`**。
2. `backend/app/agents/context.py:172-179` — `DeclaredBuiltinPolicy.authorizes` 对 `mcp_server` 返回 `False`。
3. `backend/app/services/agent_config_service.py:33-37` / `146-191` / `235-249` — `AgentRuntimeConfig` 与 DB 覆盖模型**无 `enabled_mcp_servers`**，`agent_config` 表无该列。
4. `backend/app/agents/skills/catalog.py:75-77` + `middlewares/skills.py:33-48` — `mcp_dependencies` 只解析不参与激活闭包。
5. `backend/app/agents/toolkits/__init__.py:8-15` + `registry.py` — 注册表只含内置工具，**无 MCP 工具装载入口**。

---

## PR-M1　MCP 客户端与连接测试（只发现工具，不执行副作用）

**目标**：管理端能对已注册的远程 MCP server 做**连通性测试**与**工具发现**，且只做 `initialize` + `tools/list`，**不执行任何工具**（对应 `mechanism-baseline.md:101` 不变量）。

### 设计决策

- **传输白名单**：`transport` 取值 `streamable_http`（默认）| `sse`。**拒绝 `stdio`**（`stdio` 仅限代码维护的系统内置项，禁止从 HTTP/DB 拼接命令——`mechanism-baseline.md:101`）。校验在 service 层，非法即 422。
- **凭证不落库**（A2）：`api_key` / 授权头不进 `mcp_servers` 表；运行时按命名约定从环境变量注入，如 `MCP_<SLUG_UPPER>_API_KEY`。`config_json` 只承载**非敏感**项：`timeout_seconds`（默认 10）、`headers_names`（仅头名，用于展示）、`tool_prefix`（可选）、`read_only_tools` / `dangerous_tools`（M2 用）。
- **工具对象按配置哈希缓存**：`config_hash = sha256(slug + server_url + json(config_json) + transport)`；哈希不变复用已发现工具对象，变更即失效（对齐文档「按配置哈希缓存」）。
- **探测结果持久化**：把最近一次探测结论写回 `mcp_servers.last_status` / `last_error` / `last_tested_at` / `discovered_tools`，供列表页展示（避免每次都打外部网络）。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/pyproject.toml` | `:6-32` | 增依赖 `mcp>=1.9`、`langchain-mcp-adapters>=0.1` |
| `backend/app/agents/mcp/__init__.py` | 新增 | 包导出 |
| `backend/app/agents/mcp/client.py` | 新增 | `McpClientFactory`：按 `McpServerDTO` 构造 `langchain_mcp_adapters` 客户端（`streamable_http`/`sse`），带 `timeout_seconds`；从环境变量解析凭证；配置哈希缓存 |
| `backend/app/agents/mcp/probe.py` | 新增 | `probe_server(dto) -> ProbeResult{ok, tools[], error, latency_ms}`：连接 → `tools/list`，**绝不 `tools/call`** |
| `backend/app/models/mcp_server.py` | `:16-31` | 加列 `transport`(String(32), default `streamable_http`)、`last_status`(String(32), null)、`last_error`(Text)、`last_tested_at`(DateTime)、`discovered_tools`(JSONB) |
| `backend/migrations/versions/2026xxxx_0026_mcp_transport.py` | 新增 | `down_revision="20261018_0025"`；`op.add_column` 上述列；顺带把 `mcp_servers.id` 口径与模型对齐（模型 `Integer` vs 0014 `BigInteger`，择机——不阻塞，方案内注明） |
| `backend/app/services/mcp_server_service.py` | 新增方法 | `test_connection(server_id) -> ProbeResult`（调 `probe_server` 并回写探测列）、`get_discovered_tools(server_id)`；`create/update` 增加 `transport` 校验与 `config_json` 结构校验 |
| `backend/app/routers/admin_mcp_router.py` | 新增端点 | `POST /{server_id}/test`（admin 写权限，触发外部连接）、`GET /{server_id}/tools`（admin/manager 读）；`McpServerModel` 增列；`transport` 入参校验 |
| `apps/admin-web/src/api/mcpServer.ts` | 新增 | `testMcpServer(id)`、`listMcpTools(id)`；类型补 `transport`/`last_status`/`last_error`/`discovered_tools` |
| `apps/admin-web/src/views/McpServersView.vue` | 改造 | 新增「测试连接」按钮（行内 + 详情）、工具清单抽屉、`transport` 选择、`config_json`（超时等）编辑表单、状态列展示最近探测结果 |

### 测试

- 单测 `backend/tests/test_mcp_probe.py`：假客户端断言 `probe_server` **只调用 `tools/list`、从不 `tools/call`**；`transport=stdio` 被拒（422）；配置哈希变化使缓存失效。
- 集成 `backend/tests/integration/test_mcp_servers_api.py`：CRUD + `/test` 成功/失败（外部不可达落 `last_status=failed`、`last_error` 留痕）+ 授权矩阵（无 token 401、staff 403、manager 读 200、manager 写 403、admin 全通过）。

### 验收
- 管理端可对一个真实远程 MCP server 完成「测试连接」并看到工具清单；不可达时状态显式失败且有错误文案。
- `mcp_servers` 表**零凭证明文**（沿用 A2 扫描）。

### 风险 / 依赖
- 低-中。新增外部依赖需锁版本并复跑 CI 三项检查；`langchain-mcp-adapters` 与 `langgraph~=1.0`/`langchain-core 1.6` 的兼容性需在 PR 内实测（不兼容则降级到直接用 `mcp` SDK 写薄适配，见「待拍板 D-MCP-1b」）。

---

## PR-M2　运行期接线：MCP 工具注入 + 授权快照

**目标**：把「管理端启用的 MCP server」真正变成 Agent 可见、可审计、可禁用的工具。

### 设计决策

- **资源快照放行**：`DeclaredBuiltinPolicy` 增 `mcp_servers: frozenset[str]` 与放行分支；`_default_resource_policy`（`run_body.py:347-363`）增 `mcp_servers` 参数；`run_body` 构造 `ResourceRef(kind="mcp_server", id=slug)`（**启用集合来自 `AgentConfig.enabled_mcp_servers`，None 回退代码声明**，与现有 `enabled_*` 同构）。
- **工具装载在装配期，不在执行期**：每个 Run 装配时，按授权快照里的 server slug 从 `mcp_servers` 表读 active 项 → 经 `McpClientFactory` 取工具 → 转成 LangChain `BaseTool` → 注册进 **Run 级注册表**（不污染进程级 `default_registry()`，避免跨 Run/租户泄漏）。
- **工具命名防冲突**：MCP 工具统一加前缀 `{slug}__{tool_name}`（`config_json.tool_prefix` 可覆盖），确保与内置工具名不撞（`registry.register` 重名即抛）。
- **单工具禁用**：`config_json.disabled_tools: [name...]`，装载时剔除；禁用项不入 ToolNode，模型不可见也不可调用（fail-closed，与内置工具一致）。
- **安全默认**：MCP 工具默认 **`dangerous=True`**（外部系统副作用，走 `tool_approval` 挂起审批）**除非**该工具在 `config_json.read_only_tools` 显式声明。理由：MCP 工具是外部不可信边界，默认收紧符合「失败关闭优先」原则。
- **全链复用**：MCP 工具经**同一个 `build_tool_node`**（`toolkits/registry.py:182`）包装 → 自动获得 tool_audit 事实、`CredentialGuard` 打码、`ToolGuard` 幂等、审批 `interrupt`、事件投影。**不新增旁路**。
- **Skill 依赖闭包**：`SkillCatalog.dependencies()`（`skills.py:33-48`）展开 `mcp_dependencies`——Skill 激活时开放其声明的 MCP server 工具。
- **失败语义**：server 不可达时，该 Run 的 MCP 工具**不装载**并写一条装配期告警日志（不阻断 Run；内置工具照常）。是否让「声明必须存在但探测失败」显式失败，见「待拍板 D-MCP-2」。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/app/models/agent_config.py` | `:23-30` | 加列 `enabled_mcp_servers`(JSONB) |
| `backend/migrations/versions/2026xxxx_0027_agent_config_mcp.py` | 新增 | `op.add_column("agent_config", ...)` |
| `backend/app/services/agent_config_service.py` | `AgentRuntimeConfig` `:33-37`、`load_runtime_config` `:235-249`、`save_agent_config` `:146-191`、`get_agent_detail` `:126-144` | 加 `enabled_mcp_servers` 字段的读写与 DTO 透出 |
| `backend/app/routers/admin_agent_config_router.py` | `:78` 附近、`:219` | 请求体与响应体加 `enabled_mcp_servers` |
| `backend/app/agents/context.py` | `:159-179`、`:212-217` | `DeclaredBuiltinPolicy` 增 `mcp_servers` 与 `authorizes` 分支 |
| `backend/app/services/run_body.py` | `:153-176`、`:347-363` | 构造 mcp ref；`_default_resource_policy` 增参；装配 Run 级工具注册表并把 MCP 工具并入 |
| `backend/app/agents/runtime.py` | `:94` 附近 | `RuntimeContext` 透出「本 Run 已装载的 MCP 工具」或直接并入 `tool_registry` |
| `backend/app/agents/skills/catalog.py` | `:75-77` | `dependencies()` 返回值与消费侧扩展 `mcp_dependencies` |
| `backend/app/agents/middlewares/skills.py` | `:33-48` | 激活闭包纳入 MCP 依赖 |
| `apps/admin-web/src/api/agentConfig.ts` | `:44` 附近 | 类型补 `enabled_mcp_servers` |
| `apps/admin-web/src/views/AgentConfigView.vue` | 改造 | 增加「MCP 服务器」多选（与工具/技能并列），渲染 `mcp_dependencies` |

### 测试

- 集成 `backend/tests/integration/test_agent_mcp_tools.py`（真实图，依赖 PR-M3 的 gateway 或测试内起的本地 MCP server）：
  - 正例：写 `AgentConfig.enabled_mcp_servers=[gateway]` → 运行 Run → **模型可见** `gateway__lock_issue_credential` → 调用 → 回读 `tool_audit` 归属与状态。
  - 负向：未启用该 server → 工具**不可见**；`disabled_tools` 命中 → 不可见；`read_only_tools` 未声明的工具 → 调用时触发**审批挂起**而非直接执行。
  - 凭证边界：MCP 工具回包含密码 → `CredentialGuard` 打码后落库零明文。

### 验收
- 审计事实里能看到 MCP 工具的调用记录（`tool_audit.tool_name` 带前缀、归属子 Agent）。
- 未启用的 MCP 工具在模型请求里不出现（负向取证）。

### 风险 / 依赖
- **中-高**：触及 `run_body` 装配主链与 `context.py` 授权策略，需回归 `test_run_config_wiring.py`、`test_agent_runtime.py`。装配期引入网络 I/O，**必须设短超时 + 全兜底**，避免拖慢 Run 启动。

---

## PR-M3　`mcp_gateway` 独立服务（lock / robot / wechat 以 MCP 暴露）

**目标**：让 PR-M1/M2 有**本地可自证的运行时对象**；把 `design.md`/`mechanism-baseline.md` 声明的网关落地。

### 设计决策

- 新增 `backend/app/mcp_gateway/main.py`：用官方 `mcp` SDK 的 **streamable_http** server（独立 uvicorn app），复用**既有渠道客户端**（`backend/app/lock/`、机器人/支付渠道）作为工具实现，**不重写业务逻辑**。
- 三个 server（可合并为一个进程多路由，或按子路径区分）：`lock-mcp`（凭证生成/轮换/吊销/开锁记录）、`robot-mcp`（配送下单/状态）、`wechat-mcp`（登录/支付回调状态）。`pms-mcp` 可选。
- **与 §7 不变量一致**：封装只做协议转换，业务服务、幂等、失败语义**不变**（对齐 `mechanism-baseline.md:280` 的边界声明）。gateway 内部仍走 `device_sim`（模拟器契约），真机联调留 P4.1/P9。
- `deploy/docker-compose.yml` 增 `mcp-gateway` 服务（默认 profile，端口如 `9200`），`api`/`worker`/`task-worker` 无需新环境变量（gateway 作为被管理端注册的远程 MCP）。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/app/mcp_gateway/__init__.py` / `main.py` | 新增 | MCP server（streamable_http），工具转发到现有渠道客户端 |
| `backend/app/mcp_gateway/tools.py` | 新增 | 工具定义（名称/描述/schema）与到 `app/lock/**` 等的调用适配 |
| `deploy/docker-compose.yml` | `:148` 附近 | 新增 `mcp-gateway` 服务 |
| `backend/app/seed.py` 或新迁移 | — | 预置 3 条 `mcp_servers` 记录（指向 gateway），便于一键注册 |
| `backend/app/config.py` | `:86` 附近 | 新增 `mcp_gateway_base_url`（默认 `http://localhost:9200`），供 seed/演示 |

### 测试

- 单测 `backend/tests/test_mcp_gateway.py`：`tools/list` 契约稳定（名称/描述/schema 快照）；工具执行正确转发到渠道客户端；渠道失败时 MCP 错误语义可辨。
- 端到端（集成）：注册 gateway → 配 `enabled_mcp_servers` → Run 调用 `lock` 工具 → 回读 `lock_credentials` / `tool_audit` 事实一致。

### 验收
- 本地 `docker compose up mcp-gateway` 后，管理端「测试连接」能发现三个 server 的工具；一次真实 Run 能经 MCP 完成一次凭证类调用。

### 风险 / 依赖
- 中。依赖 PR-M1（客户端）先行；gateway 若有状态，注意与 `device_sim` 的状态隔离。

---

## PR-M4　验收与文档

- `docs/acceptance-matrix.md` 新增主张 **A9 — MCP 服务器运行时生效**：四段式（主张/失败面/直接证据/负向案例）+ 证据清单 + 负向取证（stdio 拒绝、未启用不可见、单工具禁用、连接失败不执行副作用、凭证零落库）。**证据落地时回填**，不预置空行。
- 更正 `docs/mechanism-baseline.md §2.7`：把「✅ 声明」改为按交付事实陈述（传输白名单、探测只发现工具、配置哈希缓存、`app/mcp_gateway/` 落地位置），标注阶段与已知边界。
- `docs/remaining-work-plan.md` 第三层登记 MCP 子项；`docs/design.md` §Agent 运行时设计核对措辞。
- README「MCP 服务器经管理后台启用与配置」段落补「含运行时装载与连通性测试」的可自证表述。

---

## 1. 依赖顺序与建议排期

```
PR-M1（客户端 + 探测 + CRUD 增强）
   └─→ PR-M3（gateway，提供可自证对象）──┐
                                        ├─→ PR-M2（运行期接线 + 授权快照）
                                        │      └─→ PR-M4（验收与文档）
                                        └──────┘
```
- M1 是 M2/M3 的前置；M3 与 M2 可并行，但 M2 的端到端自证需要 M3（或测试内起的本地 MCP server）。
- 建议合并顺序：**M1 → M3 → M2 → M4**。

## 2. 每个 PR 的统一验收口径

```
ruff check / ruff format --check / mypy app     全绿（CI 要求）
pytest -q -m "not integration"                  单测绿
pytest -q -m integration                        集成绿（需 PostgreSQL + Redis；gateway 类需 compose）
admin-web: npm run lint + type-check + build    前端绿
触及的验收主张在 acceptance-matrix.md 回填证据行
```

## 3. 已拍板决策（2026-10-18）

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| D-MCP-1 | MCP 做到多深 | **全链路真接**（运行时装载 + 工具发现 + 单工具禁用 + 自建 gateway） |
| D-MCP-2 | 客户端实现 | **官方 `mcp` SDK + `langchain-mcp-adapters`** |

## 4. 仍需你拍板的子决策

| 编号 | 决策点 | 我的建议 |
| --- | --- | --- |
| D-MCP-1b | 若 `langchain-mcp-adapters` 与 langgraph 1.0 不兼容 | 退回「官方 `mcp` SDK + 自写 ~80 行薄适配」，仍属标准 SDK，不降级为自研协议 |
| D-MCP-2 | MCP 工具默认危险级别 | **默认 `dangerous=True`（走审批）**，`read_only_tools` 显式豁免 |
| D-MCP-3 | 声明的 server 探测失败时 | **该 Run 跳过其工具并告警日志**，不阻断 Run（与「失败关闭」的取舍：这里取可用性，留痕不静默） |
| D-MCP-4 | gateway 是否含 `pms-mcp` | 暂**不含**（PMS 房态同步非核心），留占位 |