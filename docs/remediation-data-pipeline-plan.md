# 数据管道「清洗规则补齐 + 失败治理」PR 级实施步骤

状态：已交付（2026-10-18；PR-D1–D6，验收 A10 已回填）
日期：2026-10-18
上游：[remediation-plan.md](./remediation-plan.md) P3.3 · [mechanism-baseline.md](./mechanism-baseline.md) §2.15 · [remaining-work-plan.md](./remaining-work-plan.md)
范围：PR-D1 运行记录可靠性（4 个真 bug） · PR-D2 规则种子与参数化 · PR-D3 真实调度器 · PR-D4 数据湖前端页 · PR-D5 报表预聚合新鲜度守卫 · PR-D6 验收与文档

> 用户已拍板：**建真实调度器**；一并纳入**数据湖前端页**、**报表预聚合新鲜度守卫**、**验收矩阵新增主张**。

---

## 0. 现状实测（本机 `haochen-postgres-1`，2026-10-18）

```
data_cleaning_rules:  2 条
  1 | test          | data_pipeline.clean_conversations | cron=- | enabled=true
  3 | audit_archive | data_pipeline.archive_audit_logs   | cron=- | enabled=true

data_pipeline_runs:   7 次
  completed 1 (14%)   failed 4 (57%)   running 2 (29%，永不收敛)
  真实错误：HTTPConnectionPool(localhost:9000) Max retries ×2（MinIO 不可达）
            No module named 'pyarrow'              ×2
            2 条 run 无终态（扣在 running）
```

**「规则少」的根因**：`data_cleaning_rules` **无任何种子**（`app/seed.py` 无 `DataCleaningRule`），空库需人工在界面逐条建；且界面**无法设置 `config` 参数**（`DataPipelineView.vue` 弹窗只有 name/task_type/description/schedule_cron），只能吃 handler 默认值。
**「失败多」的根因**：见 PR-D1 的 4 个真 bug —— 不是偶发，是**确定性失败**（只要触发就复现）。

---

## 0.1 本轮对上游/文档的修正（细读代码后）

| 项 | 直觉判断 | 细读后的修正 | 影响 |
| --- | --- | --- | --- |
| 规则 | 「清洗规则已有，数量少」 | 代码定义 **9 个管道**（handler），但**规则表行**需自建且无种子；前端 `PIPELINE_TYPES` 硬编码 9 项 | 缺的是「开箱即用的规则 + 参数可见」 |
| 调度 | 「`schedule_cron` 字段在，应能定时」 | 全仓 grep `schedule_cron` 仅命中 model/service/router 的**写入与展示**，**零读取执行点** | 定时调度从未实现，PR-D3 补 |
| 失败 | 「失败率高」 | 线上 4 次 failed 中 2 类是环境（MinIO/pyarrow），2 条是**代码 bug 导致的永不收敛**；`archive_audit_logs` 与 `export_*` 有**必然失败/必然全量重导**的真 bug | 先修代码，再谈失败率 |
| 前端 | 「数据管道页已交付」 | 页面在，但运行记录「管道」列显示逻辑反了（`DataPipelineView.vue:346`）；数据湖后端 5 端点**零前端消费** | PR-D1/D4 修 |

**「运行失败」的代码层根因（file:line）**
1. **checkpoint 键永不匹配 → export 每次全量重导**：`_get_checkpoint(ctx, task_type)`（`task_handlers.py:583-588`）用 task_type 查 `DataPipelineRun.source_table`，但完成时写的是 `source_table="messages"`（`:715`）/ `"model_audit,tool_audit"`（`:776`）→ `export_messages`/`export_audits` 的 checkpoint **恒为 None**，每轮从头导出。`export_business` 因查/存都是 `"orders"` 而**恰好正确**。
2. **`build_aggregates` 异常不写终态**：`except` 分支（`:1120-1123`）只 `logger.exception` + `return {"status":"failed"}`，**从不调 `_complete_run`** → run 永久卡 `running`；且 handler 正常返回 → `TaskExecutor` 视为成功 → tasks 表显示成功。**失败完全不可见**。
3. **`archive_audit_logs` 主键类型不匹配 + 吞异常 + 无分页**：`data_archives.source_id` 是 `BigInteger`（`models/data_pipeline.py:81`），但 tool_audit 分支取 `pk_col="run_id"`（`:311`，UUID 字符串）→ 插入必失败；异常被**按表吞掉**只 `total_errors += 1`（`:336-338`），外层仍报 `completed`；`LIMIT 1000` **无 while 循环**（`:297`）→ 每表每次最多 1000 行，积压排不空。
4. **run/task 事务竞态**：`_create_run_and_task`（`data_pipeline_service.py:99-134`）先把 `DataPipelineRun` `add` 到 caller session（未提交，`:117`），再 `submit_task`（**自持事务立即提交并派发**，`task_service.py:118-127`）→ worker 可能在 run 行提交前执行 handler，`complete_run` 的 `WHERE status='running'`（`:194-196`）匹配 0 行 → run 停在 `running`。**线上 2 条卡死即此**。
5. 附带：无并发互斥（同规则连点可重叠）；`preview_table` 用 f-string 拼表名（`admin_datalake_router.py:126`，已被 `_register_tables` 限定为湖中已有表，风险有限但应参数化）。

---

## PR-D1　运行记录可靠性（修 4 个真 bug）

**目标**：让「每次运行都有确定终态、失败可见、导出增量正确、归档真能推进」。这是**失败率治理的地基**。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/app/data_pipeline/task_handlers.py` | `:583-588` | `_get_checkpoint` 改为**按 pipeline run 的 `rule_id` 或显式 `checkpoint_scope`** 查最近成功 `summary.checkpoint`，**与完成时写入的 `source_table` 解耦**。最小改法：完成时把 checkpoint 存进 `summary["checkpoint"]` 并让 `_get_checkpoint` 用**同一 task_type 常量**匹配（或直接在 `summary` 里带 `task_type` 字段再查）。修完 `export_messages`/`export_audits` 才真正增量 |
| 同上 | `:1120-1123` | `build_aggregates` 的 `except` 分支补 `_complete_run(ctx, status="failed", error=str(e), duration_seconds=...)`；成功/跳过分支也补 `_complete_run(status="completed"/"skipped", summary=results)`——**每个出口都写终态** |
| 同上 | `:284-338` | `archive_audit_logs`：① `source_id` 统一取真实主键列 `id`（`model_audit`/`tool_audit` 都有 `id`，见 `models/audit.py:29,86`）；② 逐表 while 分批直到无行（配合 `LIMIT`）；③ **区分「表级失败」与「成功」**——任一表抛错则整体 `status="failed"` 并写 `error`，不再伪装 completed；④ 支持 `dry_run` |
| `backend/app/services/data_pipeline_service.py` | `:99-134` | `_create_run_and_task` 改为**先在 caller session `commit` run 行，再 `submit_task`**；或让 `submit_task` 接受外部 session 在同一事务内落 task 行、**提交后再派发**（对齐 `submit_task` 既有「先落库后投递」不变量）。消除竞态 |
| 同上 | `:136-156` | `trigger_pipeline` 增**并发互斥**：以 `dedupe_key=f"pipeline:{rule_id}"` 提交（复用 `submit_task` 的活跃去重），或在 service 侧拒绝「同规则已有 running run」 |
| `backend/app/routers/admin_datalake_router.py` | `:126` | `preview_table` 改用参数化/白名单校验表名，不拼 f-string |
| `apps/admin-web/src/views/DataPipelineView.vue` | `:346` | 修「管道」列显示：应从 `record.task_type`（需后端响应补该字段）或规则映射取中文名，而非 `record.summary ? null : record.task_id` |

### 测试（当前此模块**零测试覆盖**，本 PR 一并补齐）

- 单测 `backend/tests/test_data_pipeline_handlers.py`（用替身 session/ctx）：
  - `build_aggregates` 抛错时 **run 落 failed**（回归 bug 2）。
  - `archive_audit_logs` 对 tool_audit **写入成功**（回归 bug 3），且分批推进。
  - `_get_checkpoint` 在完成记录写 `source_table="messages"` 后**能命中**（回归 bug 1）。
- 集成 `backend/tests/integration/test_data_pipeline_api.py`：规则 CRUD + 触发 + `complete_run` 竞态（触发后立即查 run 终态必为 running→completed/failed，不再永卡）+ 授权矩阵。

### 验收
- 同一规则连续触发 N 次，`data_pipeline_runs` **无一条停留在 running**；失败必带 `error`。
- `export_messages` 第二次运行的 `processed_rows` 显著小于首次（增量生效）。

### 风险 / 依赖
- 低-中。改 `_get_checkpoint` 的匹配键**只影响 export 系列**，需回归一次全量导出以确认 checkpoint 语义；事务顺序调整需回归 `task_service` 既有测试。

---

## PR-D2　规则种子与参数化

**目标**：空库即有一组**可用的默认规则**；界面能设 `config` 参数。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/app/seed.py` | 新增 `_seed_cleaning_rules` | 预置 **9 条**规则（对应 9 个 handler），带**生产默认 cron**与合理 `config`：<br>`clean_conversations` `0 3 * * *` `{batch_size:500}`<br>`dedup_feedback` `0 4 * * *`<br>`archive_audit_logs` `0 5 * * *` `{retention_days:90,tables:[model_audit,tool_audit]}`<br>`validate_kb_consistency` `0 6 * * 1` `{freshness_days:180}`<br>`clean_qa_labels` `0 7 * * *` `{confidence_threshold:0.6}`<br>`export_messages` `*/30 * * * *`<br>`export_audits` `*/30 * * * *`<br>`export_business` `*/30 * * * *`<br>`build_aggregates` `10 * * * *` |
| `backend/app/services/data_pipeline_service.py` | `create_rule` | 增 `task_type` **白名单校验**（必须是已注册 handler），否则 422 |
| `backend/app/routers/admin_data_pipeline_router.py` | `RuleCreate/Update` | `config` 增结构校验（可空对象；未知键容忍但留存） |
| `apps/admin-web/src/views/DataPipelineView.vue` | 弹窗 | 增 `config` 参数编辑（按 `task_type` 显示已知参数：`batch_size`/`retention_days`/`tables`/`confidence_threshold`/`dry_run` 等）；`dry_run` 开关 |
| `apps/admin-web/src/api/dataPipeline.ts` | — | 类型补充 |

> 注：`build_aggregates` 的依赖是 `export_*` 先跑过；种子 cron 相位错开（export 每 30 分钟、aggregate 每小时 10 分），避免空跑。

### 测试
- 单测：种子幂等（重复 seed 不重复插入）；白名单校验拒绝未知 `task_type`；`config` 非法形状被拒。

### 验收
- 全新库 `python -m app.seed` 后，`data_cleaning_rules` 有 9 条启用规则；界面能看到并编辑其 `config`。

---

## PR-D3　真实调度器（`schedule_cron` 生效）

**目标**：让管理端填的 cron 真正触发管道，`trigger="scheduled"` 成为真实取值。

### 设计决策

- **调度载体**：在 **task-worker** 加一条 ARQ cron（对齐 `app/worker/settings.py:145-153` 既有「基础设施类周期任务」写法），**每分钟**扫描 `data_cleaning_rules`：解析 `schedule_cron`，命中的、启用的、且**本分钟未触发过**的规则 → `DataPipelineService.trigger_pipeline(rule_id, trigger="scheduled")`。
- **幂等/去重**：用 `dedupe_key=f"pipeline:{rule_id}:{cron_minute_bucket}"`（如 `YYYY-MM-DDTHH:MM`）提交任务，重启补发不重复（复用 `submit_task` 的活跃去重）；避免与手动触发重叠（PR-D1 的互斥兜底）。
- **cron 解析**：用 **`croniter`**（轻量、成熟）解析 5 段 cron；非法表达式在**规则保存时**即 422（PR-D2 补校验），运行时不静默跳过。
- **避免扫描风暴**：规则量小（个位数~几十），每分钟一次 `SELECT` 可接受；如需，加 `next_run_at` 缓存列（按增量优化，本 PR 不做）。
- **可观测**：调度扫描失败（DB 不可达等）不影响 worker 其它 cron；每次提交落 `trigger="scheduled"` 的 run，与手动触发可区分。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/pyproject.toml` | `:6-32` | 增依赖 `croniter>=2.0` |
| `backend/app/worker/settings.py` | `:145-153` | `TaskWorkerSettings.cron_jobs` 增 `cron(submit_pipeline_schedules_job, minute=set(range(0,60)), second={20})`（与既有相位错开） |
| `backend/app/data_pipeline/scheduler.py` | 新增 | `submit_pipeline_schedules_job(ctx)`：扫规则→`croniter` 判命中→`trigger_pipeline(trigger="scheduled")`；返回提交计数 |
| `backend/app/services/data_pipeline_service.py` | 新增 | `list_due_rules(now)` / `trigger_pipeline(..., trigger="scheduled")` 已支持 |
| `backend/app/routers/admin_data_pipeline_router.py` | `RuleCreate/Update` | `schedule_cron` 保存时用 `croniter` 校验，非法 422 |
| `apps/admin-web/src/views/DataPipelineView.vue` | 规则表 | 「定时调度」列补 cron 可读化（如 `0 3 * * *` → 「每天 03:00」）+ 「下次运行」提示（可选） |

### 测试
- 单测 `backend/tests/test_pipeline_scheduler.py`：给定 `now` 与规则集，命中判定正确（`* * * * *` 必命中、`0 3 * * *` 仅 03:00 命中）；非法 cron 保存被拒；去重键在**同一分钟重复调用**只提交一次。
- 集成：设一条 `* * * * *` 规则 → 手动调 `submit_pipeline_schedules_job` → 断言生成 `trigger="scheduled"` 的 run 且 `rule_id` 正确。

### 验收
- 配置 `0 3 * * *` 的规则在次日 03:00 自动产生一次 `scheduled` 运行（或测试内以 `now` 注入验证）。
- worker 重启后不产生重复调度（去重键生效）。

### 风险 / 依赖
- 中。依赖 PR-D1（终态可靠）与 PR-D2（规则种子）先行，否则调度会放大「卡 running / 必然失败」。多副本 task-worker 时需保证同一分钟只有一个副本提交——**去重键 + `get_active_by_dedupe_key` 已提供此保证**（`task_service.py:90-93`）。

---

## PR-D4　数据湖前端页

**目标**：把后端已有的 `/admin/datalake/*`（分区浏览 / 即席查询 / 房型月度聚合）搬上管理端。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `apps/admin-web/src/api/datalake.ts` | 新增 | `listDatalakeTables`、`listPartitions(table)`、`queryDatalake(sql,limit)`、`roomTypeMonthly(from,to)`、`previewTable(table,limit)` |
| `apps/admin-web/src/views/DataPipelineView.vue` | 增 tab | 新增「数据湖」tab：表清单 → 选中表看分区/预览；即席 SQL 查询框（**只读提示 + 行数上限**）；房型月度入住率/客单价表格（`month_from/to` 筛选） |
| `apps/admin-web/src/router/index.ts` / `theme/navIcons.ts` | — | 若独立成页则加菜单；若并入管道页则无需（建议**并入管道页 tab**，减少导航项） |

### 测试
- 前端 `npm run type-check` + `build`；后端已有 `test_datalake_query_service.py` 覆盖服务层，本 PR 仅加前端消费。

### 验收
- 管理端可选择数据湖表、预览行、跑只读 SQL、查房型月度指标；湖不可达时页面显式降级提示（不白屏）。

### 风险 / 依赖
- 低。纯前端消费既有接口；注意即席 SQL 的只读白名单（`datalake_query_service` 已有 `_ALLOWED_PREFIXES`/`_BLOCKED_KEYWORDS`）。

---

## PR-D5　报表预聚合新鲜度守卫

**目标**：消除 `remediation-plan.md:242` 登记的 A4 张力——残留/陈旧 `agg_*` 分片令 token/tool 指标**静默归零**。

### 设计决策

- **默认走 PG、预聚合需显式开关**（推荐，最小惊讶）：`Settings` 增 `report_agg_enabled: bool = False`；`report_service` 的 `token_totals`/`token_usage_by_model`/`token_usage_by_day`/`cache_hit_series`/`performance`（`report_service.py:349/387/417/454/514`）在开关关闭时**一律走 PG 重算**。
- **开关打开时加新鲜度校验**：`_duckdb_query_agg` 返回前，比对聚合分片的**生成时间**（Parquet 写入时间 / 或 `agg_*` 分区日期）与报表窗口；分片早于窗口末尾（或多于 N 小时未更新）即**判为陈旧 → 返回 None → 回落 PG**，并记一条告警。
- **可观测**：陈旧降级计数进 `docs/operations/slo-oncall.md` 的告警/指标口径。

### 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `backend/app/config.py` | 报表段附近 | 增 `report_agg_enabled: bool = False`、`report_agg_max_staleness_seconds: int = 3600` |
| `backend/app/services/report_service.py` | `:180-212`（`_duckdb_query_agg`）+ 5 个消费方法 | 开关关闭直接返回 None；开启时加新鲜度校验；陈旧降级留痕 |
| `docs/operations/slo-oncall.md` | — | 补「预聚合陈旧降级」指标与处置 |
| 报表/健康集成测试 | `tests/integration/test_report_service.py`、`test_admin_reports_api.py` | 补陈旧分片用例：伪造旧分片 → 断言指标**不归零**（回落 PG） |

### 验收
- 存在陈旧 `agg_token_daily` 分片时，报表 token 指标**仍与 PG 重算一致**（不再恒为 0）。
- 开关打开 + 分片新鲜时，走预聚合（快路径）；陈旧时自动降级并告警。

### 风险 / 依赖
- 中。触及报表读路径，需回归 A4 相关集成测试；默认关闭开关会**改变既有行为**（从「预聚合优先」变「PG 优先」），须在方案内明确这是**修正**而非回退。

---

## PR-D6　验收与文档

- `docs/acceptance-matrix.md` 新增主张 **A10 — 数据管道可靠调度与失败可见**：四段式 + 证据清单 + 负向取证（卡 running 回归、失败不伪装成功、增量导出、并发不重叠、cron 去重）。**证据落地时回填**。
- 更正 `docs/mechanism-baseline.md §2.15`：补「调度器」「失败语义」「预聚合新鲜度守卫」。
- `docs/remaining-work-plan.md` 第三层登记数据管道子项；`docs/eval/datalake-benchmark.md` 若受 D5 影响则同步口径。
- README「数据清洗管道与数据湖导出」段落更新为「可调度、失败可见、增量导出」的可自证表述。

---

## 1. 依赖顺序与建议排期

```
PR-D1（终态可靠，地基）
   └─→ PR-D2（规则种子 + 参数）──→ PR-D3（真实调度器）
   └─→ PR-D4（数据湖前端页，与 D2/D3 无强依赖，可并行）
   └─→ PR-D5（报表新鲜度守卫，独立）
                                          └─→ PR-D6（验收与文档）
```
- **D1 必须最先**（否则 D3 会放大现存失败）。D2→D3 串行。D4/D5 可与 D2/D3 并行。D6 最后。
- 建议合并顺序：**D1 → D2 → D3 →（D4 ∥ D5）→ D6**。

## 2. 每个 PR 的统一验收口径

```
ruff check / ruff format --check / mypy app     全绿（CI 要求）
pytest -q -m "not integration"                  单测绿（本模块此前无覆盖，本轮补齐）
pytest -q -m integration                        集成绿（需 PostgreSQL + Redis；MinIO 不可达按契约降级）
admin-web: npm run lint + type-check + build    前端绿
触及的验收主张在 acceptance-matrix.md 回填证据行
```

## 3. 已拍板决策（2026-10-18）

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| D-DP-1 | 定时调度 | **建真实调度器**（task-worker cron + `croniter` + 去重键） |
| D-DP-2 | 附加范围 | **纳入**数据湖前端页、报表预聚合新鲜度守卫、验收矩阵新增主张 |

## 4. 仍需你拍板的子决策

| 编号 | 决策点 | 我的建议 |
| --- | --- | --- |
| D-DP-3 | 报表预聚合默认策略 | **默认走 PG**（`report_agg_enabled=false`），预聚合作显式快路径 |
| D-DP-4 | `build_aggregates` 的调度频率 | 每小时一次（`10 * * * *`），依赖 export 相位在前 |
| D-DP-5 | 规则种子的 cron 是否写死 | 写**生产合理默认**但**全部可编辑**；`enabled` 默认 true（区别于 export 类是否默认关，见下） |
| D-DP-6 | 导出入湖类规则（`export_*`/`build_aggregates`）默认启用否 | **默认启用但 cron 相位错开**；若你希望更保守，可默认停用待手动开 |