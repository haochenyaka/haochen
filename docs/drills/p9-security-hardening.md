# P9 安全加固记录

状态：done（P9.2 交付物）
日期：2026-09-30
相关文档：[P9 可靠性演练记录](./p9-reliability-drill.md)、[development-plan.md](../development-plan.md)、[design.md](../design.md)

本记录是 P9.2 的交付物：在已有安全基座上完成剩余的加固项。

## 已有安全基座（P0–P8 已交付）

| 安全层 | 位置 | 状态 |
| --- | --- | --- |
| 认证（Bearer 令牌 + Redis 会话） | `app/security/identity.py`, `session_service.py` | 故障关闭：Redis 不可用时登录 503、认证 401 |
| RBAC 角色守卫 | `app/security/identity.py` `require_roles()` | 路由依赖级：admin/manager/staff/customer/operator |
| 密码哈希 | `app/security/passwords.py` | argon2id 单向哈希 |
| 速率限制（Redis 滑动窗口） | `app/security/rate_limiter.py` | 按组分级（guest/auth/admin/chat），故障开放 |
| 运行时密码打码 | `app/agents/middlewares/credential_guard.py` | 持久化边界：消息/审计/检查点/记忆零明文 |
| ToolGuard 幂等键 | `app/agents/middlewares/tool_guard.py` | 拦截重复下单/改密 |
| 全表明文扫描 | `tests/integration/test_p4_security_audit.py` | 7 张业务表零密码明文（含 orders/payments/messages） |
| 越权负向测试 | `tests/integration/test_authorization_negative.py` | 14 个负例全部通过（A6 取证） |
| 敏感信息扫描器 | `app/agents/middlewares/credential_guard.py` | 可检出 password/id_card/phone 三类敏感明文 |

## P9.2 新增加固项

### 1. CORS 中间件

**文件变更：** `backend/app/main.py` + `backend/app/config.py`

生产部署时管理后台 Web 与 API 不同域，需允许跨域请求：

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # 默认 ["*"]，部署时限制
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

| 项 | 值 |
| --- | --- |
| 配置键 | `cors_origins`（环境变量 `CORS_ORIGINS`） |
| 默认值 | `["*"]`— 部署时建议限制为具体域名 |
| 风险 | 生产环境不应使用通配符，需配合认证头使用 |

### 2. 基础设施密钥轮换

**涉及文件：**
- `backend/app/models/domain.py` — `LockCredential.key_version` 字段
- `backend/app/lock/service.py` — `hash_lock_code` 新增 `key_version` 参数
- `backend/app/config.py` — `lock_master_key_previous` / `lock_master_key_active_version`
- `backend/migrations/versions/20260930_0019_lock_credential_key_version.py` — 迁移
- `backend/app/lock/rotation_task.py` — `rotate_lock_master_key` DurableTask

**设计要点：**
- `code_hash = HMAC-SHA256(key, room_no:version:key_version:code)` — key_version 纳入散列输入
- `key_version` 列默认 1，已有数据自动继承版本 1
- 旧密钥保留在 `lock_master_key_previous` 中，仅用于验证旧凭证
- `rotate_lock_master_key` DurableTask 自增版本号并记录事件，密钥材料变更需手动更新环境变量
- 已有凭证不受影响（仍用签发时的 key_version 验证）

**不变量：**
- 新签发凭证始终使用当前活跃密钥（`lock_master_key`）
- 旧凭证验证时使用 `lock_master_key_previous`（若设置）回退
- 密钥轮换不改变已签发凭证的 code_hash

### 3. 明文扫描覆盖扩展

**文件变更：** `tests/integration/test_p4_security_audit.py`

新增 3 个集成测试：

| 测试 | 覆盖范围 | 断言 |
| --- | --- | --- |
| `test_admin_responses_plaintext_scan` | Admin API 响应体（memories、conversations） | 零密码类别明文命中 |
| `test_password_not_leaked_in_admin_user_endpoints` | `POST /api/admin/users` 不回显密码，DB 存 argon2id 哈希 | 响应无 `password` 字段，DB 哈希格式 `$argon2id$` |
| `test_lock_leak_response_contains_plaintext_by_design` | 泄露处置 API `POST /admin/lock/rooms/{room_no}/credential/rotate` | 响应含 `code`（明文，一次性返回），旧凭证 revoked，新凭证 active |

**扫描结果：** 所有 Admin API 响应体零密码明文泄漏（密码泄露处置端点为故意设计的例外）。

### 4. 越权测试扩展（P7/P8 端点覆盖）

**文件变更：** `tests/integration/test_authorization_negative.py`

新增 5 个越权测试，覆盖 P7/P8 新增的管理端点：

| 测试 | 覆盖的端点 | 断言模式 |
| --- | --- | --- |
| `test_admin_memory_endpoints_role_enforcement` | Memory 读/写 | Manager 可读、admin 可写、其他角色 403 |
| `test_admin_agent_config_role_enforcement` | Agent 配置读/写 | Manager 可读、admin 可写、其他角色 403 |
| `test_admin_quality_eval_role_enforcement` | 评分卡 + 双评估 | Manager 可读、staff/guest 403 |
| `test_admin_feedback_role_enforcement` | 反馈列表 | Manager 可读、staff 403、匿名 401 |
| `test_admin_hotel_config_write_role_enforcement` | 酒店配置写 | Manager 可读不可写、admin 可写 |

**越权范围：** 原有 14 个 P2 负例 + 新增覆盖约 20 个 P7/P8 端点，所有负例断言数据不被改写（通过 `_snapshot` 前后对比）。

## 速率限制认证

速率限制已在 P1 实现（`app/security/rate_limiter.py` + `tests/test_rate_limiter.py` 单元测试）。
P9.3 新增 HTTP 级集成测试 `test_drill_rate_limiter_integration`：
- 25 次快速请求 `/api/health`（5s 窗口限 10 次）→ 至少 12 次 429
- `Retry-After` 头验证

## 安全回归摘要

| 测试套件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `test_rate_limiter.py`（单元） | 12 | 路由映射、限流键、配置边界、Redis 降级 |
| `test_p4_security_audit.py`（集成） | 6 | 全表明文扫描 + Admin API 扫描 + 用户密码 + 泄露处置 |
| `test_authorization_negative.py`（集成） | 11 | 14 个 P2 负例 + P7/P8 端点 5 个角色测试 |
| `test_credential_guard.py`（集成） | 1 | 运行时打码（消息/审计/检查点/事件） |
| `test_auth_flow.py`（集成） | 1 | 会话生命周期与过期 |
| `test_p9_reliability_drill.py`（集成） | 1 (D8) | 速率限制 HTTP 级验证 |

## 证据清单

- CORS 配置：`backend/app/main.py` 第 85–92 行
- 密钥版本迁移：`backend/migrations/versions/20260930_0019_lock_credential_key_version.py`
- 密钥轮换任务：`backend/app/lock/rotation_task.py`
- 密钥版本字段：`backend/app/models/domain.py` 第 108 行
- 散列函数更新：`backend/app/lock/service.py` 第 120–125 行
- 扫描测试扩展：`tests/integration/test_p4_security_audit.py` 第 472–589 行
- 越权测试扩展：`tests/integration/test_authorization_negative.py` 第 428–590 行
- 速率集成测试：`tests/integration/test_p9_reliability_drill.py` 第 169–245 行
- 全量回归（2026-09-30）：单元测试 + 集成测试全绿。