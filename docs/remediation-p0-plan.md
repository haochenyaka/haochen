# P0「地基接线」PR 级实施步骤

状态：待评审（draft）
日期：2026-10-12
上游：[remediation-plan.md](./remediation-plan.md) 第 Phase 0 节
范围：P0.1 配置链路 · P0.2 记忆接线 · P0.3 敏感类别可配置 · P0.4 自研理由文档化 · P0.5a steer · P0.5b 审批挂起（**决策后纳入**）

---

## 0. 本轮对上游方案的修正（细读代码后）

| 项 | 上游判断 | 细读后的修正 | 影响 |
| --- | --- | --- | --- |
| P0.3 | 「隐私脱敏只覆盖密码，要补齐 id_card/phone」 | **能力已存在**：`credential_guard.py:50-54` 已定义 password/id_card/phone 三类，`mask_sensitive_plaintext` 全支持；只是 `DEFAULT_MASK_CATEGORIES`（`:80`）默认只开 password | 从「补能力」改为「**扩默认类别 + 治理误伤**」 |
| P0.4 | 「收敛重造的框架轮子」 | **前提不成立**：三个工位各有正当理由（见 PR-0.4） | 从「收敛实现」降级为「**文档化自研理由**」，不改行为 |
| P0.1 | 「接通 config_json」 | `config_json` 当前是**扁平 **传 `from_config`；`config_json.context/.middleware` 是未实现的注释 | 需先**定义配置结构**（本 PR 内决策） |
| P0.5 | 「steer + approval 补真」 | approval 的 interrupt/resume 跨 worker 边界，工作量远超其余四项 | **拆分**：P0.5a steer（小） / P0.5b approval（大，建议独立里程碑） |

---

## PR-0.1　接通管理端配置链路

**状态：✅ 已完成并通过验证（2026-10-13）**

验证证据：
- `ruff format` + `ruff check`（改动 5 文件）→ `All checks passed!`
- `mypy app` → 改动文件无新增类型错误（仓库既有 85 个错误在其它文件）
- `pytest tests/test_agent_config_normalize.py` → **4 passed**
- `pytest tests/integration/test_run_config_wiring.py tests/integration/test_agent_runtime.py` → **7 passed**
- `admin-web`: `npm run lint`（0 error）+ `type-check` + `build` → 通过

**目标**：管理端保存的 `system_prompt` / `model` / `temperature` / `enabled_*` 在运行时真正生效。

### 根因
- `app/services/run_body.py:131` 传 `config={}`，`:137` 传 `middleware_config={}`（注释「P2 接入」）。
- `app/services/run_body.py:110-116` 资源 refs 只用 `agent.tool_ids` / `knowledge_base_ids` / `skill_ids` 静态声明，忽略 `AgentConfig.enabled_*`。
- `AgentConfig.config_json`（`app/models/agent_config.py:20`）当前扁平传入 `BaseContext.from_config`（`app/agents/context.py:88-95`），**没有** middleware 段的容器。

### 决策：`config_json` 结构约定（本 PR 内定）
采用**兼容式嵌套**，避免破坏既有扁平数据：

```jsonc
// 新格式（推荐写入）
{
  "context":   { "system_prompt": "...", "model": "...", "temperature": 0.3 },
  "middleware": { "summarization": { "threshold_tokens": 30000 },
                  "credential_guard": { "mask_categories": ["password","id_card","phone"] } }
}
```

- 读取逻辑：若 `config_json` 含 `"context"` 键 → 取 `config_json["context"]` 传 `config=`，`config_json["middleware"]` 传 `middleware_config=`；否则（旧扁平数据）整段当作 `context`，middleware 取空。两者都保留向后兼容。
- `context` 段仍由 `FrontDeskContext.from_config` 做未知键失败（`context.py:92-94`）。
- `middleware` 段由 `MiddlewareConfig.from_mapping` 做未知工位失败（`pipeline.py:127-129`）。

### 改动清单
| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `app/services/agent_config_service.py` | 新增 | 加 `load_runtime_config(slug) -> (context: dict, middleware: dict, enabled: tuple)`；集中结构解析与兼容 |
| `app/services/run_body.py` | `:91-123` | 装配前调用该 loader；`config=` 传 context 段、`middleware_config=` 传 middleware 段 |
| `app/services/run_body.py` | `:110-116` | refs 构建改用 `enabled_tools/skills/knowledge_bases`（为 None 时回退 `agent.*` 静态声明） |
| `app/agents/buildin/front_desk/agent.py` | `:86-110` | `build_graph` 已用 `runtime.config`/`runtime.middleware_config`，确认无遗漏即可 |
| `apps/admin-web/src/views/AgentConfigView.vue` | 视情况 | 若前端仍在写扁平 config_json，改为写嵌套（否则走旧兼容路径也能用） |

### 测试
- 新增 `backend/tests/integration/test_run_config_wiring.py`：
  1. 写一条 `AgentConfig(config_json={"context": {"system_prompt": "OVERRIDE_SENTINEL"}})`，跑 Run，断言 `model_audit` 对应请求命中覆盖值。
  2. 写 `enabled_tools` 收窄后，断言 `ResourceSnapshot.tools` 只含收窄后的工具。
  3. 旧扁平 `config_json={"system_prompt": "..."}` 仍生效（兼容）。
- 复用 `test_agent_runtime.py` / `test_run_worker.py` 的 fake 模型与内存 checkpointer。

### 验收
- 管理端改 `temperature` → 下次 Run 生效，有测试证据。 ✅
- `docs/acceptance-matrix.md` 增行。 ⬜（待补）

### 风险 / 依赖
- 低。独立可先做。**是 PR-0.2 / PR-0.5a 的前置**（它们从 middleware 段读开关）。

---

## PR-0.2　接通长期记忆工位（读/注入）

**状态：✅ 代码完成，自身测试通过（2026-10-13）**

改动：`RuntimeContext` 增 `memory_provider` 字段（TYPE_CHECKING 导入避免成环）；`MemoryStation`「注入来源即启用」（配置开关或 provider 二者其一）；`DbMemoryProvider` 改持 `sessionmaker` 短开短读；`run_body` 构造并注入；`front_desk/agent.py` 传入流水线。

验证：`test_memory_injection.py`（记忆真注入 + 敏感条目过滤）、`test_middlewares.py`（更新默认关闭/自动启用断言）→ **34 passed**；ruff/mypy 无新增问题。

**已知既有问题（非本 PR 引入，已用 `git stash` 对照证明）**：
- `test_p4_security_audit.py::test_p4_plaintext_scan_across_all_tables` 批量跑偶发失败——全库扫描未按本测试数据过滤，共享库其它行污染。**已修**（按 room_id/order_id/payload 过滤，见提交 `25ba0da`）。
- `test_room_service.py::TestCheckoutFlow::test_verify_and_complete_revokes_credential` 在 `test_lock_rotation` 之后跑失败（`409 凭证版本不匹配`）——根因是 `lock_rotation`/`room_service` 两个测试模块共享全局 lock-task-handler 注册表（`importlib.reload` + 模块级 `LockClient` 注入）导致替身状态串扰。**未修**（需改测试夹具，独立于本轮改动，待单独处理）。

**全量 integration 单会话跑的既有基线（已 stash 对照，2026-10-13）**：
- WITHOUT 本轮改动：**21 failed / 272 passed**；WITH 改动：**19 failed / 274 passed**（多 2 个 = 新增测试）。
- 结论：**全量单会话 integration 跑本身脆弱**（共享库累积污染 + 部分测试对全库/全表重算），失败数在 19–21 间非确定浮动，**与本轮改动无关**；CI 应保证每个集成测试的数据隔离，或分片跑。

**目标**：`MemoryStation` 在运行时真正把 `GuestMemory` 注入模型请求，敏感条目被过滤。

### 根因
- `app/agents/middlewares/memory.py:35` `MemorySettings.enabled=False` 默认；`:66-67` 启用时要求 provider + user_id + thread_id。
- `app/agents/middlewares/pipeline.py:286` 的 `MemoryStation` 未收到 `memory_provider`。
- `DbMemoryProvider`（`app/services/admin_memory_service.py:166`）**已实现** `MemoryProvider`，但 `__init__` 收的是 `AsyncSession` 实例，而 pipeline 在 `build_graph` 内一次性构建，不适合持有一个长生命周期 session。

### 改动清单
| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `app/services/admin_memory_service.py` | `:166-189` | `DbMemoryProvider` 改为持有 `async_sessionmaker`，`load()` 内自开会话（短开短关），与工位「每次按需读」一致 |
| `app/services/run_body.py` | `:124-141` | 构造 `DbMemoryProvider(session_factory=ctx.session_factory)`，放入 `RuntimeContext`（新增字段或复用） |
| `app/agents/runtime.py` | `RuntimeContext` | 新增可选字段 `memory_provider`（与 `skills`/`workdir` 同级） |
| `app/agents/buildin/front_desk/agent.py` | `:93-103` | `build_middleware_pipeline(... memory_provider=runtime.memory_provider)` |
| `app/agents/middlewares/memory.py` | `:33-36` | `enabled` 默认值由「全局 middleware 配置」驱动（见 PR-0.1），缺失时保持 False |

### 边界（本 PR 不做）
- **记忆写入/抽取链路属 Phase 1（P1.4）**，本 PR 只做**读 + 注入**，保持小步。

### 测试
- 新增 `backend/tests/integration/test_memory_injection.py`：
  1. 预置一条 `GuestMemory(sensitive=False)` → 跑 Run → 断言模型请求消息含记忆块（`memory.py:83` 的插入位置）。
  2. 预置一条 `sensitive=True` → 断言被 `DbMemoryProvider` 过滤（`admin_memory_service.py:182`）且不注入。
  3. 预置一条含手机号的 `content` → 断言被 `_is_clean`（`memory.py:87`）整条丢弃。

### 验收
- 集成测试绿；`docs/acceptance-matrix.md` 增行。

### 风险 / 依赖
- 中低。依赖 PR-0.1（开关来源）。`DbMemoryProvider` 签名变更需同步改 `admin_memory_service` 的其它调用方（`MemoriesView` 用的是 `AdminMemoryService` 本身，不受影响）。

---

## PR-0.3　敏感类别扩默认 + 误伤治理

**状态：✅ 已完成并通过验证（2026-10-13）**

验证证据：
- `ruff check`（改动 3 文件）→ `All checks passed!`
- `pytest tests/test_credential_mask.py tests/test_middlewares.py` → **34 passed**
- `pytest tests/integration/test_credential_guard.py` → **1 passed**
- `pytest tests/integration/test_p4_security_audit.py` → **6 passed**（明文扫描验证"打码后不落库"）

**目标**：把身份证/手机号纳入默认脱敏，且不误伤正常业务话术。

### 根因
- `credential_guard.py:80` `DEFAULT_MASK_CATEGORIES=("password",)`。
- 三类 pattern 已具备（`:50-54`）；`mask_sensitive_plaintext`（`:115-139`）已支持按类别打码。
- 打码作用于四条边界：工具回包（`:200`）、助手消息（`:192`）、上下文历史（`:181`）、工具入参审计（`:168`）。

### 子决策（**已拍板 2026-10-12**）
默认三类全开（password / id_card / phone），但 **phone 采用"中间打码"**：保留前 3 位与后 4 位，只打码中间 4 位。

- 理由：手机号一旦出现在**任何**边界都部分打码（信息最小化），同时**保留可读性**——酒店电话、叫车电话仍能辨认前缀/后四位，不误伤正常业务话术，无需按边界分层。
- 形态：`138****5678`（掩码字符待定，见 D-P0-3b）。
- **代价（需接受，见 D-P0-3a）**：中间 4 位打码后，**客人仍能看到自己的完整周边信息**（前3后4 可辨认），即对"用户本人"的保护是弱的；这是"最小化 + 可读"取舍的必然。若目标是"彻底不可用"，须改全掩码。

### 改动清单
| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `app/agents/middlewares/credential_guard.py` | `:50-55` | `SENSITIVE_PATTERNS` 每种形态的**捕获组**拆成「前缀 / 中间 / 后缀」三段（`id_card` 同理：前 6 后 4 保留、中间 8 打码） |
| `app/agents/middlewares/credential_guard.py` | `:115-139` | 重写 `mask_sensitive_plaintext` 的替换函数：按类别决定保留/打码区间；`scan_sensitive_plaintext`（命中判定）**不变**（仍整串检出一个匹配） |
| `app/agents/middlewares/credential_guard.py` | `:73-78` | `PLACEHOLDER_BY_CATEGORY` 不再适用整值替换 → 改为「按捕获组重组」的掩码函数；password 仍整值 `[已打码]` |
| `app/agents/middlewares/credential_guard.py` | `:80` | `DEFAULT_MASK_CATEGORIES` → `("password","id_card","phone")` |
| `app/config.py` | 新增 | 全局默认类别 + 掩码字符，供 `MiddlewareConfig` 默认值取用 |

### 测试
- 扩展 `backend/tests/integration/test_p4_security_audit.py`：全表明文扫描断言**完整身份证/手机号**零命中（即打码后不再构成完整形态）。
- 扩展 `backend/tests/integration/test_credential_guard.py`：
  - 正向：`13812345678` → `138****5678`；身份证 `110101199001011234` → `110101********1234`。
  - 负向：助手回复中的酒店电话被部分打码后，仍能展示（不再有"该不该打"的边界纠结）。
  - 边界：长度不足、非手机号串（如订单号）不误匹配。

### 风险 / 依赖
- 低。依赖 PR-0.1（让类别可在管理端调）。**phone 打码不再是"要不要"而是"怎么掩"**，比原分层方案更简单，去掉了一个 PR 级分支。

---

## PR-0.4　自研中间件的「理由文档化」（不改行为）

**状态：✅ 已完成（2026-10-13，纯文档）**

交付：`docs/mechanism-baseline.md` 新增 §3.1「为什么三个工位自研而非直接用框架能力」，逐工位登记理由 + 代码锚点（PatchToolCalls / ModelRetry / TokenUsage）；同时更正 §3 中因 PR-0.3 失效的「默认只打码密码字段」描述。

**目标**：为三个被质疑"重造轮子"的工位留下站得住的理由，供面试/评审时讲清。

### 细读后的结论：**不应收敛，应解释**
| 工位 | 看似重造 | 实际必要理由（代码证据） |
| --- | --- | --- |
| `patch_tool_calls.py` | LangGraph 应自带消息修复 | 项目用**检查点 + 数据库历史重放**（`run_body.py:104-105,143-144`），摘要折叠/外部写入会留悬空 `tool_calls`，OpenAI 兼容接口直接 400；框架无等价修复点（`:1-6` 注释），自研合理 |
| `model_retry.py` | `ChatOpenAI` 自带 `max_retries` | 模型层**显式关掉** `max_retries=0`（`models.py:111`），为的是让**每次尝试各自写审计事实**（`model_retry.py:3-4`）；框架重试不落审计 |
| `token_usage.py` | LangChain 有 `usage_metadata` | 它**同时**做两件事：Provider 实际用量（`extract_usage`）+ **近似上下文估算**（`estimate_text_tokens`，`:30-34`）供**摘要阈值**判断（`summary.py` 依赖）；近似估算是框架没有的 |

### 改动清单
| 文件 | 改动 |
| --- | --- |
| `docs/mechanism-baseline.md` | 新增「为什么自研而非用框架能力」小节，逐工位写理由 + 代码锚点 |
| （可选）`app/agents/middlewares/{patch_tool_calls,model_retry,token_usage}.py` | 仅在确有可清理的重复时微调，**不删能力** |

### 验收
- 一份可引用的设计说明；面试时能一句话答"为什么不用框架自带"。

### 风险 / 依赖
- 无。纯文档，可随时做。

---

## PR-0.5a　steer 工位补真（取消/接力）

**状态：✅ 已完成并通过验证（2026-10-13）**

改动：`SteerStation` 由空转改为「配置驱动、默认关闭」——启用后模型调用前查线程是否已排入更新请求，有则置取消信号并中断本 Run；`RunContext.cancel_event` 暴露公开属性、`RuntimeContext.cancel_event` 贯通、`run_body`/`front_desk` 接线；`run_worker` 显式按取消收敛 `SteerSuperseded`。

验证：`test_steer.py`（两条消息 → run1 cancelled + run2 接力派发）通过；`test_middlewares.py` 默认关闭断言不变；运行时回归 58 passed；ruff/mypy 无新增问题。

**关键决策**：启用采用**配置驱动**（`middleware.steer.enabled`），非"接线即启用"——避免隐式改变默认 Run 行为（客人连发两条时默认仍"先答完再答下一条"）。

**目标**：`SteerStation` 从空转变成"发现更新的用户消息 → 中止当前 Run、让新消息接力"。

### 根因
- `app/agents/middlewares/steer.py:16` `SteerSettings.enabled=False`，`:19-26` 无任何方法覆写，`:1-4` 注释「随 T9 接入」。

### 可复用的既有能力（好消息）
- 取消意图已有：`agent_request_queue_service.py:174-178` `request_cancel(run_id)` → run 置 `cancel_requested`。
- 取消复核已有：`run_worker.py:89-95` `ensure_not_cancelled`、`:235-237` 心跳发现 `cancel_requested` 置 `cancel_event`。
- 接力已有：`thread_dispatch.promote_head_if_idle`（`thread_dispatch.py:22-37`）。

### 改动清单
| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `app/agents/middlewares/steer.py` | 整文件 | 实现 `prepare_model_call`：若线程在本 Run 启动后有更新的 queued request（查 `RequestRepository`），抛/置取消信号 + 注入「用户已发新消息，请尽快收尾」提示 |
| `app/services/run_worker.py` | `:145-152` | 把 `cancel_event` 暴露给工位（经 `RuntimeContext`），steer 触发时 set |
| `app/agents/runtime.py` | `RuntimeContext` | 新增 `cancel_event`（或回调）字段 |
| `app/agents/middlewares/steer.py` | settings | `enabled` 由 middleware 配置驱动（PR-0.1） |

### 测试
- 新增 `backend/tests/integration/test_steer.py`：Run 进行中提交第二条消息 → 断言当前 Run 收敛为 `cancelled`、新 Run 接力执行。

### 验收
- steer 从「配置可开但无行为」变为「有测试覆盖的真实行为」。

### 风险 / 依赖
- 中。依赖 PR-0.1、PR-0.5a 需要 `run_worker` 协作。竞态点：取消意图 vs 正常完成的边界，复用现有 `lease_lost`/`cancel_event` 判定（`run_worker.py:160-169`），不新造语义。

---

## PR-0.5b　tool_approval 接 interrupt/resume（**已纳入 P0**）

**目标**：危险工具调用时真正暂停等待人工审批，审批通过后恢复执行，而非把 Run 弄失败。

### 根因
- `app/agents/middlewares/tool_approval.py:82` 默认策略只 `raise ToolApprovalRequired` 直接失败；`base.py:34-40` 注释「interrupt/resume 随 T9 接入」。

### 为什么它比其余四项大（跨 worker 边界的挂起/恢复）
需要在 5 个层面同时落子：

| 层 | 改动 | 文件 |
| --- | --- | --- |
| 状态机 | 新增 `awaiting_approval` Run 状态 + 迁移 | `app/models/runtime.py`、`migrations/versions/` 新迁移 |
| 图 | 用 LangGraph `interrupt()` 挂起（依赖已有 `AsyncPostgresSaver` 检查点） | `app/agents/middlewares/tool_approval.py` |
| 恢复入口 | 管理端审批 → 以 `Command(resume=...)` 从检查点续跑 | 新增 admin 路由 + `app/services/run_body.py`（支持 resume 路径） |
| 接力协调 | 挂起期间线程是否放行新请求（现约束「单线程单活跃 Run」） | `app/services/thread_dispatch.py:22-37` |
| 事件 | SSE 新增「等待审批」投影事件 | `app/services/run_events.py`、`run_stream.py` |

### 改动清单（初稿）
| 文件 | 改动 |
| --- | --- |
| `app/models/runtime.py` + 新迁移 | `AgentRun.status` 增 `awaiting_approval`；新增 `tool_approval_request` 表（run_id, tool_name, args_digest, status, decided_by, decided_at） |
| `app/agents/middlewares/tool_approval.py` | `before_tool` 改为：`require_approval` 时调用图级 `interrupt({...})`，落审批请求事实 |
| `app/services/run_body.py` | 支持从检查点 resume：管理端审批后以 `Command(resume={"approved": bool})` 恢复图 |
| `app/services/run_worker.py` | `awaiting_approval` 的 Run 不进入终态收敛；租约/心跳策略适配（挂起期可能超过 `LEASE_SECONDS=60`） |
| `app/services/thread_dispatch.py` | 决定挂起期线程语义：**建议挂起即释放线程锁**，让新请求排队但不抢占该 Run |
| `app/routers/admin_*.py` | 新增审批端点（列出待审批 + 批准/拒绝） |
| `apps/admin-web/src/views/` | 新增/复用审批界面 |

### 关键设计决策（**待你拍板，见 D-P0-5b**）
1. **挂起期是否释放线程**：建议「释放锁但不取消 Run」——新请求排队，审批完成后该 Run 先续跑还是让队头先跑，需定优先级。
2. **审批超时**：挂起多久自动拒绝/转人工？（建议与转人工坐席打通）
3. **哪些工具默认危险**：目前靠注册元数据 `dangerous` 标记（`tool_approval.py:22`），需确认现有工具里哪些打了标。

### 测试
- 新增 `backend/tests/integration/test_tool_approval_interrupt.py`：
  1. 危险工具 → Run 挂起 `awaiting_approval`，SSE 出等待事件。
  2. 审批通过 → 恢复执行 → completed，且工具结果正确回灌。
  3. 审批拒绝 → Run 收敛 denied（非 failed）。
  4. 挂起期间提交新消息 → 排队不抢占（验证线程语义）。

### 验收
- 三个分支（通过/拒绝/超时）各有测试；`docs/acceptance-matrix.md` 增行。

### 风险 / 依赖
- **高**（P0 内最高）。依赖 PR-0.1（配置）、且与 Phase 1 的 supervisor interrupt 需求高度重叠——**建议实现时按可复用边界设计**，避免 P1 重做。
- 注意向上游 memory `haochen-remediation-plan` 同步该 PR 已从"独立里程碑"改为"纳入 P0"。

---

## 1. 依赖顺序与建议排期

```
PR-0.1（配置链路） ─┬─→ PR-0.2（记忆接线）
                    ├─→ PR-0.3（敏感类别，中间打码）
                    ├─→ PR-0.5a（steer）      ← 需 run_worker 协作
                    └─→ PR-0.5b（审批挂起）    ← 最重，依赖配置 + worker
PR-0.4（理由文档）  ─── 独立，随时
```

建议合并顺序：**0.1 → 0.4 → 0.3 → 0.2 → 0.5a → 0.5b**（先做无依赖/低风险的，0.1 是一切前置，0.5b 最后做且按可复用边界设计，供 Phase 1 supervisor 复用）。

## 2. 每个 PR 的统一验收口径
- `ruff check` / `ruff format --check` / `mypy app` 绿（CI 已要求，见 `.github/workflows/ci.yml`）。
- 新测试进 `pytest -q -m "not integration"` 或 `-m integration`，CI 全绿。
- 触及的验收主张在 `docs/acceptance-matrix.md` 回填证据行。

## 3. 已拍板决策（2026-10-12）

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| **D-P0-3** | 手机号打码范围 | **三类全默认打码；身份证前6后4保留、手机号前3后4保留（中间打码）** |
| **D-P0-1** | `config_json` 结构 | **改为嵌套 `{context, middleware}`**，兼容旧扁平 |
| **D-P0-5b** | 审批挂起 | **纳入 P0**，作为 PR-0.5b |

## 4. 仍需你拍板的子决策

| 编号 | 决策点 | 我的建议 |
| --- | --- | --- |
| **D-P0-3a** | 中间打码保留了前3后4 → 客人仍可辨认自己的完整号码，"对本人保护弱"是否接受？ | 接受（最小化 + 可读的取舍）；若要求"彻底不可用"则改全掩码 |
| **D-P0-3b** | 掩码字符用 `*` 还是 `****`（4 位对齐，如 `138****5678`） | `****` 定长 4 位，保持长度与可读 |
| **D-P0-5b-1** | 审批挂起期是否释放线程锁 | 释放锁但不取消 Run；新请求排队 |
| **D-P0-5b-2** | 审批超时策略 | 超时转人工坐席（与现有 handoff 打通） |
| **D-P0-5b-3** | 现有工具里哪些标记为 `dangerous` | 需先盘点注册表元数据 |