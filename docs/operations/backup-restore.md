# 备份与恢复 Runbook（P4.5）

- 状态：已自动化（PostgreSQL 逻辑备份 + 异地副本 + CI 恢复演练）
- 目标：单店数据 RPO ≤ 24 小时、RTO ≤ 2 小时
- 适用：`deploy/docker-compose.yml` 的 `backup` 服务与 `deploy/backup.sh` / `deploy/restore.sh`

## 1. 覆盖范围

| 数据 | 备份方式 | 说明 |
| --- | --- | --- |
| PostgreSQL | `deploy/backup.sh` 逻辑备份（custom format） | 订单、会话、审计、配置、同意记录等权威事实 |
| MinIO 对象 | 基础设施卷快照/对象存储版本化 | 知识库原始文件；**未纳入逻辑备份** |
| Milvus 向量 | 可从 PostgreSQL 分块与原始文件重建 | 不单独备份；重建任务见知识库运营流程 |
| Neo4j 图 | 种子/投影数据可重建 | 当前为样例与投影数据，不单独备份 |
| Redis | 不需要 | 会话、限流、事件流均为可重建的短生命周期数据 |

> 上线前必须为 MinIO 配置独立的对象备份或版本化策略；否则知识库原始文件丢失只能重新上传。该缺口登记在 `launch-readiness-checklist.md`。

## 2. 定时与异地

`backup` 服务随 compose 启动，启动后立即执行一次，之后每 `BACKUP_INTERVAL_SECONDS`（默认 86400 秒）执行一次：

```bash
docker compose -f docker-compose.yml up -d backup
# 生产：
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d backup
```

| 变量 | 默认 | 生产要求 | 说明 |
| --- | --- | --- | --- |
| `BACKUP_INTERVAL_SECONDS` | 86400 | 按 RPO 调整 | 两次备份的最大间隔 |
| `BACKUP_RETENTION_DAYS` | 30 | 30 | 本地与异地副本同时保留 |
| `BACKUP_ENCRYPTION_KEY` | 空 | **必填** | 仅经环境变量/KMS 注入，禁止写入文件 |
| `BACKUP_REQUIRE_ENCRYPTION` | false | **true** | 未配置密钥时备份直接失败 |
| `OFFSITE_BACKUP_DIR` | `./backups-offsite` | **必填** | 必须是境内、不同故障域的挂载 |
| `BACKUP_REQUIRE_OFFSITE` | false | **true** | 未配置异地目录时备份直接失败 |

生产覆盖层用 `${VAR:?}` 强制要求密钥与异地目录，缺少时 `docker compose` 直接报错，不会带病启动。

异地建议：独立 NAS/NFS、另一台宿主机、或对象存储网关的独立桶。**不能**与 `BACKUP_DIR` 同盘。

## 3. 产物、加密与校验

每个备份产生三个文件：

```
haochen_<UTC时间>.dump.gz.enc   # 加密后的 custom-format 备份
haochen_<UTC时间>.dump.gz.enc.sha256
backups/manifest.txt           # 追加式清单：时间、库名、大小、校验和、是否加密
```

- 加密：`openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt`；密钥通过 `-pass env:` 读取，不出现在进程参数中。
- 备份完成后会解密回读并 `gzip -t` 校验，再计算 SHA-256。
- 异地副本复制后立即用 `.sha256` 复核。
- 保留期外文件按 `-mtime +N -delete` 清理，本地与异地分别执行。

## 4. 监控与告警

当前没有内置的「备份新鲜度」告警，运营侧必须配置外部监控：

1. 检查 `backups/manifest.txt` 最后一条的 `time`，超过 26 小时未更新即告警。
2. 检查 `backup` 容器日志是否出现 `本次备份失败`；容器保持运行并在下一周期重试。
3. 检查异地目录最新文件时间与本地一致。
4. 建议将上述检查接入现有告警通道，并在 `docs/operations/slo-oncall.md` 增加值班处置项。

## 5. 恢复步骤

### 5.1 恢复到新库（推荐）

```bash
# 先确认备份文件与校验和
ls -l backups/haochen_*.dump.gz.enc*
cd deploy

BACKUP_ENCRYPTION_KEY=... \
DB_HOST=postgres DB_PORT=5432 DB_USER=haochen DB_PASS=... DB_NAME=haochen \
bash restore.sh /backups/haochen_20261018T030000Z.dump.gz.enc haochen_restored --yes
```

`restore.sh` 会：

1. 校验 `.sha256`；
2. 按 `.enc` / `.gz` / 裸 dump 自动选择解密/解压；
3. 创建目标库（不存在时）；
4. `pg_restore --clean --if-exists --exit-on-error`；
5. 校验 `rooms/orders/messages/agent_runs` 是否存在并输出行数（可用 `RESTORE_VERIFY_TABLES` 覆盖）。

### 5.2 切换到恢复库

1. 对比恢复库与事故前的关键事实（订单数、最近消息、审计行数）。
2. 在维护窗口内把 `DATABASE_URL` 的库名切到 `haochen_restored`，重启 `api` / `worker` / `task-worker`。
3. 保留原库至少一个备份周期，确认无误后再下线。
4. 记录恢复时间、备份文件、校验和、核对结果与操作人。

### 5.3 生产红线

- 不直接在 `haochen` 生产库上执行 `restore.sh`；先恢复到新库并校验。
- 恢复前确认目标库名，脚本会拒绝非 `[A-Za-z0-9_]` 名称并默认交互确认。
- 加密备份缺少 `BACKUP_ENCRYPTION_KEY` 时脚本失败，不会产出半恢复状态。

## 6. CI 恢复演练

`.github/workflows/ci.yml` 的 `backup-restore` job 在每次 PR/push 执行：

1. 启动 PostgreSQL service，建 4 张演练表并写入数据；
2. 放置一个 40 天前的过期文件，验证保留期清理；
3. 执行 `deploy/backup.sh`，开启加密与异地要求，断言 `.enc`、`.sha256`、异地副本与 manifest 均存在；
4. `DROP TABLE ... CASCADE` 模拟数据丢失；
5. 执行 `deploy/restore.sh ... haochen_restore --yes`，断言恢复后行数一致。

本地无 `pg_dump/pg_restore` 时该演练以 CI 为准；本机集成测试中的 D7 仍保留在 `backend/tests/integration/test_p9_reliability_drill.py`。

## 7. 演练与取证

| 频率 | 动作 | 记录 |
| --- | --- | --- |
| 每次 PR | CI `backup-restore` job | GitHub Actions 日志 |
| 每月 | 在预发环境执行一次恢复到新库 | 恢复记录（时间、备份文件、行数、耗时） |
| 每季度 | 模拟主库不可用，按 §5 完整切换 | 演练报告，更新 RTO |
| 变更后 | 调整备份参数/密钥后重跑 CI 与一次手工恢复 | 变更单 |

## 8. 已知限制

- 只覆盖 PostgreSQL；MinIO 原始文件需单独备份。
- 密码含 `@`、`:` 等字符时 URL 解析可能失败；容器内优先使用 `DB_USER/DB_PASS/DB_HOST/DB_PORT/DB_NAME` 逐项注入。
- 备份服务是固定间隔而非 cron 定点；如需「每天 03:00」，改用宿主机 cron/systemd timer 调用同一脚本。
- 备份新鲜度告警需外部监控接入。
