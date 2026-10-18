# 上线就绪 Checklist（Phase 4）

- 版本：2026-10-18
- 结论：**P4.2–P4.5 工程证据已齐；P4.1 改造项已关闭，但真实厂商对接、运营与合规项仍未完成，当前不具备生产上线条件。**
- 用法：发布评审逐条核对；`✅` 表示有仓库内证据，`⬜` 表示必须在上线前关闭。

## 1. 安全

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| 生产配置失败关闭（弱口令/默认密钥/通配 CORS/关闭限流/开发身份头） | ✅ | `backend/app/config.py::production_issues`、`backend/tests/test_config_production.py` |
| 生产密钥只经环境变量注入 | ✅ | `deploy/docker-compose.prod.yml` 的 `${VAR:?}` |
| 中间件与 API 不发布宿主端口，仅 nginx 暴露 80/443 | ✅ | `deploy/docker-compose.prod.yml` |
| TLS 1.2/1.3、HSTS、安全头、SSE 关缓冲、XFF 覆盖写入 | ✅ | `deploy/nginx/templates/haochen.conf.template` |
| 限流分层（IP + 用户 + 租户总额度） | ✅ | `backend/app/security/rate_limiter.py`、`backend/tests/test_rate_limiter.py` |
| 越权负向用例（客人×房间、保洁×任务、管理员×配置） | ✅ | `backend/tests/integration/test_authorization_negative.py` |
| 门锁密码只存哈希与生效窗口（A2） | ✅ | `backend/app/models/domain.py`、`backend/tests/integration/test_lock_credentials.py` |
| 客人/员工/管理员角色边界 | ✅ | `backend/app/security/identity.py`、各路由 `require_roles` |

## 2. 可观测与告警

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| Langfuse 默认不外发；self-host 或显式脱敏外发二选一 | ✅ | `backend/app/services/langfuse_service.py`、`backend/tests/test_observability.py` |
| SSE 订阅/完成/断开/异常分钟桶指标 | ✅ | `backend/app/services/observability_service.py` |
| Run 卡住、租约过期率、SSE 断流率、DB 池耗尽告警 | ✅ | `GET /api/admin/health/alerts`、`backend/tests/integration/test_admin_health_api.py` |
| SLO、处置 SQL、升级路径、季度演练 | ✅ | `docs/operations/slo-oncall.md` |
| 告警外部轮询与值班通道接入 | ⬜ | 告警为拉取式；需在监控系统配置轮询与升级 |

## 3. 可靠性与容量

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| checkpoint 连接池可配置并启动期校验 | ✅ | `CHECKPOINT_POOL_*`、`backend/tests/test_reliability_capacity.py` |
| 长会话只加载最近消息；截断显式告警并注入提示 | ✅ | `backend/app/services/run_body.py`、`backend/tests/test_run_body_reply.py` |
| 摘要阈值低于常见上下文窗口（默认 24K） | ✅ | `SUMMARY_THRESHOLD`、`docs/mechanism-baseline.md §2.9` |
| 审计事实短窗口合并写入，调用返回前提交 | ✅ | `backend/app/services/audit_service.py`、`backend/tests/test_reliability_capacity.py` |
| 备份恢复演练自动化 | ✅ | CI `backup-restore` job；见 §4 |

## 4. 备份与恢复

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| 定时逻辑备份 | ✅ | `backup` 服务、`deploy/backup.sh`、`BACKUP_INTERVAL_SECONDS` |
| 异地副本与校验 | ✅ | `OFFSITE_BACKUP_DIR`、SHA-256 复核、`docs/operations/backup-restore.md` |
| 备份加密与密钥边界 | ✅ | `BACKUP_ENCRYPTION_KEY`、AES-256-CBC(pbkdf2)；生产强制 |
| 保留期（本地 + 异地 30 天） | ✅ | `BACKUP_RETENTION_DAYS`、CI 过期文件清理断言 |
| 恢复脚本非交互、校验和、自动解密、恢复到新库 | ✅ | `deploy/restore.sh`、CI `backup-restore` |
| 备份新鲜度告警 | ⬜ | 需外部监控检查 manifest/文件时间；尚未接入告警通道 |
| MinIO 原始文件备份 | ⬜ | 逻辑备份只覆盖 PostgreSQL；需对象存储版本化/独立备份 |
| 生产异地存储为境内独立故障域 | ⬜ | 部署时由运维确认并记录 |

## 5. 隐私合规

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| 公开隐私告知（版本、用途、数据类别、保留期、出境口径） | ✅ | `GET /api/privacy/notice`、`backend/tests/integration/test_consent_api.py` |
| 同意/撤回追加式记录与版本校验 | ✅ | `consent_records`、迁移 `20261018_0025`、`POST/GET /api/privacy/consents` |
| 敏感用途单独同意（身份核验、人脸、营销） | ✅ | `backend/app/services/consent_service.py` |
| PII 明文字段守卫（证件/人脸原图与向量） | ✅ | `backend/tests/test_privacy_notice.py` |
| 手机号/证件号/密码打码 | ✅ | `backend/app/agents/middlewares/credential_guard.py` |
| 数据清单、最小必要、出境、证件/人脸存储方案 | ✅ | `docs/operations/privacy-compliance.md` |
| 小程序首次启动告知/同意页 | ⬜ | 后端 API 已就绪，前端未接入 |
| 服务端「未同意不得创建业务数据」强制 | ⬜ | 当前依赖前端门禁，需统一依赖或网关校验 |
| 自动化保留期清理与删除级联 | ⬜ | 当前为运营策略，需定时任务与审计 |
| 客人自助访问/导出/删除 | ⬜ | 当前人工工单 |
| 身份核验/证件/人脸功能 | ⬜ | 未实现；启用前完成 PIA、法务评审与 §4 建表 |
| 供应商 DPA、模型/追踪服务出境评估 | ⬜ | 法务与采购在上线前完成 |

## 6. 外部系统诚实性（P4.1 改造项已关闭，上线前仍需处理）

| 条目 | 状态 | 说明 |
| --- | --- | --- |
| 对外表述统一为「含 device-sim 契约模拟器，为真机联调预留契约」 | ⬜ | 需更新 README/宣传材料并逐处核对 |
| 真实厂商对接 checklist（微信支付 v3 证书签名、门锁协议差异、设备离线处置） | ⬜ | `docs/contracts/external-systems-v0.md` 待补 |
| 门锁/机器人失败的人工兜底与 on-call 流程 | ⬜ | 需补「半夜开不了门」等处置预案 |
| 视觉审核当前为 mock（恒通过 → 人工复核路径） | ✅/⬜ | 代码行为已按契约实现；真实视觉服务仍待接 |

## 7. 内容与数据

| 条目 | 状态 | 说明 |
| --- | --- | --- |
| 知识库替换为真实酒店资料 | ⬜ | 当前为样例内容，见剩余工作计划 |
| Neo4j 周边地点替换为门店实测值 | ⬜ | 当前为样例数据 |
| 生产门店/房型/价格初始化 | ⬜ | 需按门店导入并复核 |

## 8. 发布评审结论

可勾选上线的必要条件：

1. `⬜` 项全部关闭，或在发布单中明确风险接受人、影响范围与回滚方案。
2. CI 全绿（backend / integration / backup-restore / compose-config / 三个前端）。
3. 至少完成一次预发环境的备份恢复演练与一次客诉/隐私事件桌面演练。
4. 生产密钥由 KMS/密钥管理下发，异地备份目录与保留策略由运维签字确认。

**当前判断**：P4.2–P4.5 的工程与文档证据已具备；P4.1 改造项已关闭，但真实厂商对接、前端同意页、服务端同意强制、自动化保留期清理、MinIO 备份与供应商合规仍是上线前必须关闭的硬缺口。
