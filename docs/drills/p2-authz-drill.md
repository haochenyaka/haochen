# P2 越权负向（A6 服务端授权）取证记录

状态：done（P2-T6 交付物）
日期：2026-09-27
相关文档：[development-plan.md](../development-plan.md)（P2 §2.3/§2.6）、[design.md](../design.md)、[glossary.md](../glossary.md)（§11–§13）、[平台机制基线与取舍](../mechanism-baseline.md)

本记录是 P2-T6 的取证交付物：以真实 HTTP 请求 + 真实 PostgreSQL 验证「服务端授权」验收主张 A6 —— 资源归属在服务端判定，越权请求一律拒绝且不产生任何数据变更。取证已固化为自动化测试，可随时重放。

## 运行方式

```bash
cd backend
DATABASE_URL="postgresql+asyncpg://haochen:haochen@localhost:5432/haochen" \
  ./.venv/Scripts/python.exe -m pytest -q tests/integration/test_authorization_negative.py
```

依赖真实 PostgreSQL（集成测试会话级临时库）与 Redis（登录签发会话令牌）。用例经 ASGI 真实请求链路（`TestClient`）发起，不绕过 FastAPI 依赖注入，也不 mock 数据库。

## 受测的最小只读入口

越权负例需要真实的可访问资源，P2 因此交付两个最小只读接口作为取证入口（P4/P5 前端接入后沿用同一套服务端归属判定）：

| 接口 | 权限 | 可见性判定 |
| --- | --- | --- |
| `GET /api/staff/tasks`、`GET /api/staff/tasks/{id}` | staff 角色 | `housekeeping_tasks.assigned_staff_id == 当前账号绑定的员工档案` |
| `GET /api/guest/rooms/{room_no}/credential` | 任意已认证身份 | 客人档案本人 + 该房间在其**未终态**订单内；只回元数据（版本/生效窗口），不含 `code_hash`、设备凭证号 |
| `POST/PATCH /api/admin/*` | 写为 admin | 房间/房型/员工/账号配置（P2-T4 交付，本记录取证其负例） |

角色判定发生在路由依赖（`require_roles`），归属判定发生在服务与仓储（`StaffTaskService`、`RoomAccessService`、订单可见性查询）；两层都不依赖前端。

## 负例场景与结果

| # | 场景 | 请求 | 期望 | 结果 |
| --- | --- | --- | --- | --- |
| N1 | 跨员工任务（列表） | staff 甲 `GET /api/staff/tasks` | 仅返回本人任务，他人任务不出现 | 通过 |
| N2 | 跨员工任务（详情） | staff 甲 `GET /api/staff/tasks/{乙的任务}` | 403 | 通过 |
| N3 | 正向对照 | staff 乙 `GET /api/staff/tasks/{乙的任务}` | 200 | 通过 |
| N4 | 角色越界 | customer / manager 访问 `/api/staff/tasks` | 403 | 通过 |
| N5 | 未认证 / 伪造令牌 | 无头、`Bearer forged-token` 访问员工与凭证接口 | 401（伪造令牌不回退开发态请求头） | 通过 |
| N6 | 跨房间凭证（他人） | 客人乙 `GET /api/guest/rooms/{甲的房间}/credential` | 403 | 通过 |
| N7 | 跨房间凭证（无订单） | 客人甲请求非本单房间 | 403 | 通过 |
| N8 | 不存在房间 | 客人甲请求随机房间号 | 403（不区分「不存在」与「不属于你」，不泄露房间目录） | 通过 |
| N9 | 非客人身份 | staff 账号访问客人凭证接口 | 403（无客人档案） | 通过 |
| N10 | 凭证元数据最小化 | 客人甲请求本单房间 | 200 且响应体不含 `code_hash`/设备凭证号/哈希值 | 通过 |
| N11 | 订单终态后失效 | 本单订单流转至 `completed` 后再请求 | 403 | 通过 |
| N12 | 凭证吊销后缺失 | 凭证 `revoked` 后再请求 | 404 | 通过 |
| N13 | 非管理员配置修改 | manager / staff / customer `POST /api/admin/room-types` | 全部 403 | 通过 |
| N14 | 非管理员配置修改 | manager `PATCH /api/admin/room-types/{id}` | 403 | 通过 |

**数据不变断言**：N1–N4、N6–N9、N13–N14 在请求前后对任务（`status`/`updated_at`/`assigned_staff_id`）、凭证（`status`/`version`/`revoked_at`）、房态（`status`/`updated_at`）、配置表行数取快照并逐一比对——越权请求只读不写，拒绝路径不产生任何副作用。

## 与 P2 退出标准的对照

| 退出标准（development-plan P2） | 覆盖 |
| --- | --- |
| 负向授权用例全部拒绝 | N1–N9、N11–N14（401/403/404 各自的语义边界） |
| 负例后数据不变 | 快照比对（见上） |
| 验收主张 A6 取证 | 本记录 + 测试套件；角色判定（路由依赖）与归属判定（服务/仓储）双层落点 |
| 权限最小化（顺带取证 A2 前置） | N10：凭证接口只回元数据，明文与哈希均不出现在响应体 |

## 结论与已知边界

- 授权是服务端事实：前端隐藏按钮、角色下拉等 UI 约束不作为安全边界；三端在 P4/P5 接入时复用同一套依赖与服务。
- 拒绝语义分层：未认证/凭证失效 → 401；已认证但无权限或资源不属于该身份 → 403；资源不存在或状态不可见（如凭证已吊销） → 404。跨归属资源不区分「不存在」，避免目录探测。
- P2 的两条最小只读接口只覆盖取证所需的读取路径；员工接单/完工、客人开门密码下发等写路径在 P4/P5 落地时，沿用同一套服务端归属判定并各自补齐负例。
- 用例在单进程内并发无关（顺序请求），未覆盖多进程竞态；越权判定的正确性由「身份 → 归属查询 → 拒绝」的确定性路径保证，与并发无关。
- 集成库为会话级共享临时库，因此负例断言使用「本用例世界内的对象快照/计数」，不使用全库绝对计数。
- 开发态身份请求头（`X-Dev-Username`，可免登录取得客人身份）仅在 `DEV_IDENTITY_ENABLED` 打开时生效，且伪造 Bearer 不会回退到该请求头；容器部署在 `deploy/docker-compose.yml` 中显式默认关闭（`DEV_IDENTITY_ENABLED=false`），本地裸机开发保持默认开启。
