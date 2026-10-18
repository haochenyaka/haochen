# SLO 与 on-call 手册

适用版本：P4.3（2026-10-17）。所有时间均为 UTC；报表与告警窗口一律为半开区间。

## 1. 事实来源与边界

- **唯一权威事实是 PostgreSQL 审计表**。告警评估只读 `agent_runs` / `agent_run_attempts`，
  并对同一窗口重算，不落第二份统计副本（验收主张 A4）。
- SSE 指标是**排障计数器**，不是业务事实：Redis 分钟桶（键 `metrics:sse:<epoch-minute>`，
  默认 TTL 1 小时），Redis 不可用时降级为进程内计数，只在单副本内可见。
- 告警评估是**拉取式**：`GET /api/admin/health/alerts`。仓库不内置推送通道；由外部监控
  每 30 秒轮询该端点，按 `status` 路由到值班渠道。
- 连接池指标只覆盖**评估请求所在的 API 进程**；worker / task-worker 的池需各自观察。

## 2. SLO（初始目标，按季度基线校准）

| 指标 | 目标 | 口径 | 数据来源 |
| --- | --- | --- | --- |
| API 可用性 | 月度 ≥ 99.5% | 外部探针请求 `/api/health`，5xx 与超时计失败 | 外部监控 |
| Agent Run 成功率 | 24h ≥ 99% | `completed / (completed + failed)`，不含用户取消 | `agent_runs.status` |
| 租约过期率 | 15min < 1% | `outcome='lease_expired' / 已结束尝试` | `agent_run_attempts` |
| 首 token 延迟 | p95 ≤ 3s | `first_token_at - started_at` | `agent_runs` / 报表服务 |
| SSE 异常收流率 | 15min < 10% | （提前断开 + 服务端异常）/ 订阅数；总线读写失败单列判定 | `/alerts` 的 `sse_disconnect` |
| 数据库连接池 | < 90% | 本进程 `checked_out / pool_size` | `/alerts` 的 `db_pool_exhaustion` |
| 报表口径一致 | 100% | 报表每个数值等于对审计事实直接重算 | 验收主张 A4 |

## 3. 告警规则

阈值默认值在 `backend/app/config.py`，可用同名环境变量覆盖；`deploy/docker-compose.yml`
已透传。

| 规则 | 默认阈值 | 级别 | 含义 |
| --- | --- | --- | --- |
| `run_stuck` | pending > 300s 或 running 心跳 > 180s | 有 running 停滞时 critical，否则 warning | 派发/执行链路卡住 |
| `lease_expiry_rate` | 15min 内 ≥ 10 次已结束尝试且过期率 > 5% | critical | worker 崩溃、阻塞或数据库抖动 |
| `sse_disconnect` | 15min 内 ≥ 20 次订阅且异常率 > 10%，**或**任意事件总线读写失败 | warning | 客户端断流或 Redis 降级 |
| `db_pool_exhaustion` | 签出率 ≥ 90% | critical | 连接泄漏或数据库变慢 |

判定细节：

- Run 租约 60 秒、心跳 20 秒；reconciler 每 30 秒收敛过期租约为
  `failed(worker_lease_expired)`。因此「租约已过期」计数可能短暂出现，`run_stuck`
  以**心跳停滞**为主要触发条件，避免与自动收敛抢跑。
- `lease_expiry_rate` 样本不足时明确返回 `triggered=false` 与
  `facts.enough_samples=false`，不把低流量误判为故障。
- `sse_disconnect` 中 `follow_errors` / `read_errors` 代表 Redis 事件总线不可用、
  SSE 已退回数据库投影；这属于可感知降级，即使订阅量不足也触发。
- 所有规则都返回 `facts`，值班可直接定位到数量与窗口，无需另查统计表。

## 4. 查看告警

```bash
TOKEN="<管理员或店长会话令牌>"
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://hotel.example.com/api/admin/health/alerts | jq
```

响应结构：

```json
{
  "status": "critical",
  "evaluated_at": "2026-10-17T12:00:00Z",
  "rules": [
    {
      "rule": "run_stuck",
      "severity": "critical",
      "triggered": true,
      "value": 2,
      "threshold": 0,
      "summary": "卡死 Run 2 个：pending 超时 0 个…",
      "facts": {"pending_stuck": 0, "running_stuck": 2, "expired_leases": 1}
    }
  ]
}
```

`status`：`ok` / `warning` / `critical`。权限为 admin / manager 只读；staff / customer
一律 403。

## 5. 处置手册

### 5.0 前 5 分钟

1. 确认影响面：`GET /api/system/ready`（PostgreSQL / Redis），再取 `/alerts`。
2. 查看最近一次发布与 worker 重启记录；先止损再定位。
3. 记录时间线，不在日志、工单或聊天中粘贴密钥、客人原话或完整手机号。

### 5.1 `run_stuck`

```sql
SELECT id, status, attempt_count, created_at, started_at,
       last_heartbeat_at, lease_expires_at, owner_token, error
FROM agent_runs
WHERE status IN ('pending', 'running', 'cancel_requested')
  AND (created_at < now() - interval '5 minutes'
       OR last_heartbeat_at IS NULL
       OR last_heartbeat_at < now() - interval '3 minutes')
ORDER BY created_at
LIMIT 50;
```

- **pending 超时**：检查 run worker 是否存活、ARQ 队列是否可达。pending 是已提交的
  投递意图，repost cron 每 30 秒补发；不要手工改状态，先恢复投递链路。
- **running 心跳停滞**：worker 崩溃、事件循环阻塞或数据库不可达。租约到期后
  reconciler 自动收敛，确认 `agent_run_attempts.outcome` 与 `error_class`；必要时重启
  worker（先确认没有正在执行的 Run）。
- **不要**手工把 Run 改成 `completed`；终态只能由租约持有者或 reconciler 写入。

### 5.2 `lease_expiry_rate`

```sql
SELECT outcome, error_class, count(*)
FROM agent_run_attempts
WHERE started_at >= now() - interval '15 minutes'
GROUP BY outcome, error_class
ORDER BY count(*) DESC;
```

常见原因：worker OOM / 被重启、数据库延迟导致心跳失败、单次执行阻塞事件循环。
处理：确认容器重启次数与节点资源；检查数据库 p95 延迟；修复后观察 15 分钟窗口回落。
不要关闭 reconciler 或延长租约来「压掉」告警。

### 5.3 `sse_disconnect`

1. 看 `facts.storage_degraded`：
   - `true`：Redis 不可用或 Stream 读取异常，SSE 已退回数据库终态投影。先恢复 Redis：
     `docker compose exec redis redis-cli ping`，检查内存与连接数。
   - `false`：以客户端网络/重连为主，核对 nginx `proxy_buffering off`、长连接超时与
     移动端网络质量。
2. 客户端带 `Last-Event-ID` 重连即从断点续传；晚于运行结束的订阅由数据库补齐终态。
3. 若异常收流集中在单个地域/运营商，转交客户端/网关排查，不在服务端放宽心跳。

### 5.4 `db_pool_exhaustion`

```sql
SELECT state, count(*) FROM pg_stat_activity
WHERE datname = current_database() GROUP BY state;

SELECT pid, now() - xact_start AS age, state, wait_event_type, left(query, 120) AS query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY age DESC LIMIT 10;
```

- 先区分「流量上涨」与「连接泄漏」：前者扩容 API 或调大池；后者定位长事务/未释放会话。
- 90% 是**池容量**占用，溢出连接数见 `facts.overflow`；不要把溢出当成无限容量。
- 重启 API 会中断进行中的 SSE（客户端可续传），只作为最后手段并先摘流量。

### 5.5 Langfuse 外发边界

- 三种模式：`disabled`（无密钥或未获授权）、`self_hosted`、`masked_external`。
  启动日志会打印 `export_mode`，不打印密钥。
- 默认按外部托管处理：只配密钥不会初始化客户端。二选一：
  `LANGFUSE_SELF_HOSTED=true`（推荐，内网地址），或
  `LANGFUSE_ALLOW_EXTERNAL_EXPORT=true`（用户/会话标识 SHA-256 脱敏，score 备注丢弃）。
- 生产环境两者都未声明且配置了密钥时，`create_app()` 启动期抛
  `RuntimeError: 生产配置不安全`。按提示补齐声明，不要删除守卫。
- 本地 PostgreSQL 审计始终是权威事实；Langfuse 故障不影响 Run 执行。

### 5.6 报表预聚合陈旧降级

- **默认口径**：`REPORT_AGG_ENABLED=false`，报表一律走 PostgreSQL 重算，不存在陈旧分片问题。
- **开启快路径后**：分片最后修改时间早于 `now - REPORT_AGG_MAX_STALENESS_SECONDS`（默认 3600s）
  即判陈旧，日志输出 `预聚合分片陈旧，回落 PostgreSQL：table=... latest=...`，并改用 PG 重算。
- **处置**：先看 `data_pipeline_runs` 中 `build_aggregates` 是否连续失败（终态 `failed` 带
  `error`）或调度未触发；再查 MinIO 对应分区是否可读。修复后下一轮管道会刷新分片。
- **不要**为了消掉日志而调大阈值或关闭开关：指标会静默归零的风险大于多一次 PG 查询。
- **已知限制**：当前以降级日志为信号，未单独暴露计数器；接入外部指标时以日志计数为准。

## 6. 升级与沟通

- **P1**：全部会话不可用、大面积 Run 失败、数据库不可写 → 立即通知负责人并启动回滚评估。
- **P2**：单规则持续触发 15 分钟、性能明显退化但可降级 → 当班处理，2 小时未恢复升级。
- 交接内容：触发规则与 `facts`、影响窗口、已执行动作、当前 `status`、下次复核时间。
- 事件结束后补一条复盘：根因、检测时延、恢复动作、是否新增阈值或演练项。

## 7. 演练与证据

每季度至少执行一次故障演练，并保留端点输出与时间线：

1. 停止 Redis 30 秒：`sse_disconnect` 应因 `storage_degraded` 触发，恢复后状态回落。
2. 强制杀掉一个执行中的 worker：`lease_expiry_rate` 在 15 分钟窗口内上升，Run 收敛为
   `failed(worker_lease_expired)`。
3. 制造慢查询占满连接池：`db_pool_exhaustion` 触发，清理后回落。
4. 轮询脚本连续 24 小时采集 `/alerts`，核对无静默漏报。

## 8. 已知限制

- 拉取式评估不主动推送；外部监控缺失时告警无人接收。
- SSE 指标 TTL 1 小时；Redis 降级时为进程内计数，多副本不聚合。
- 连接池只反映 API 进程；worker 容量需独立监控。
- 告警阈值是初始值，需按真实流量与事故复盘校准。
