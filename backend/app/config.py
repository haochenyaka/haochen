from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# 仅用于本地开发的默认凭据：生产环境启动时必须显式覆盖
DEV_DATABASE_URL = "postgresql+asyncpg://haochen:haochen@localhost:5432/haochen"
DEV_NEO4J_PASSWORD = "haochen-dev-secret"
DEV_MINIO_SECRET = "haochen-dev-secret"
DEV_LOCK_MASTER_KEY = "change-me"
DEV_DEVICE_SIM_SECRET = "dev-signing-secret"

_WEAK_PASSWORDS = {
    "",
    "haochen",
    "neo4j",
    "postgres",
    "password",
    "change-me",
    "changeme",
    "dev-signing-secret",
    "haochen-dev-secret",
}
_PRODUCTION_ENV_NAMES = {"prod", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "haochen-server"
    app_version: str = "0.1.0"
    env: str = "local"
    # P4.5 隐私告知版本：同意记录必须与该版本一致；政策更新时同步递增
    privacy_policy_version: str = "2026-10-18"

    database_url: str = DEV_DATABASE_URL
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "haochen"
    minio_secret_key: str = DEV_MINIO_SECRET
    minio_secure: bool = False
    minio_bucket_knowledge: str = "haochen-knowledge"

    milvus_uri: str = "http://localhost:19530"
    milvus_collection_prefix: str = "haochen_kb"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = DEV_NEO4J_PASSWORD
    neo4j_database: str = "neo4j"
    neo4j_timeout_seconds: float = 20.0

    # 知识库嵌入：独立 endpoint；留空时回退 MODEL_BASE_URL / MODEL_API_KEY（兼容现网）
    model_embedding_name: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: int = 0
    kb_chunk_max_chars: int = 700
    kb_chunk_overlap_chars: int = 80
    kb_max_upload_bytes: int = 20 * 1024 * 1024
    # 文档级 claim 租约：必须长于单次解析/索引耗时（任务租约由 task worker 心跳续期）
    kb_claim_lease_seconds: int = 1800
    # 两段式检索：召回宽捞上限 → 重排精排条数 → 上下文预算与单文档占比上限
    kb_recall_limit: int = 30
    kb_rerank_top_n: int = 6
    kb_context_max_chars: int = 3000
    kb_context_max_chunks_per_document: int = 3
    # 重排需显式开启：优先本地 cross-encoder（RERANK_BASE_URL），其次对话模型打分，
    # 都未配置时走确定性词面重排
    kb_rerank_enabled: bool = False
    kb_rerank_timeout_seconds: float = 30.0
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model_name: str = "bge-reranker-v2-m3"
    # 关系图补充腿：仅命中结构化问题才查 Neo4j；超时或故障降级为无图事实，不阻塞 KB 检索
    kb_graph_enabled: bool = True
    kb_graph_timeout_seconds: float = 3.0

    # P1.4 长期记忆写入：Run 终态后按策略抽取候选记忆写 GuestMemory；默认关（LLM 成本）
    memory_extract_enabled: bool = False
    memory_extract_max_items: int = 5
    memory_extract_min_chars: int = 4
    memory_extract_max_chars: int = 200

    device_sim_base_url: str = "http://localhost:9100"
    device_sim_signing_secret: str = DEV_DEVICE_SIM_SECRET

    model_name: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_temperature: float = 0.2
    model_timeout_seconds: float = 60.0

    # P2.3 Agent 答案评判：与生成侧不同模型或不同温度，多次采样取中位（自一致性）。
    # 评判模型留空时复用 MODEL_NAME，但温度默认与生成温度 0.2 区分。
    agent_judge_model_name: str = ""
    agent_judge_temperature: float = 0.7
    agent_judge_samples: int = 3
    agent_judge_pass_threshold: float = 0.6

    # 上下文与中间件：阈值是压缩与卸载的唯一口径，仅按环境覆盖
    workdir_root: str = "data/workdir"
    # Skill 内容根：留空取与 app 包同级的 skills/（源码树 backend/skills，容器 /srv/app/skills）
    skills_root: str = ""
    # 摘要阈值必须低于常见 32K/64K 上下文窗口：10 万 token 会在摘要触发前先撞模型上限
    summary_threshold: int = 24_000
    summary_keep_messages: int = 10
    # 单次 Run 加载的最近消息条数；更早消息不进入上下文，截断时显式告警
    message_history_limit: int = 200
    large_result_threshold_bytes: int = 8192
    large_result_preview_chars: int = 200
    model_retry_max_attempts: int = 3
    model_retry_backoff_seconds: float = 0.5

    # 运行事件（SSE）：Redis Stream 承载投影事件，TTL 与长度是保留上限
    run_event_ttl_seconds: int = 3600
    run_event_maxlen: int = 512
    sse_heartbeat_seconds: float = 15.0
    sse_follow_block_ms: int = 2000

    # P4.4 容量：checkpoint 每进程一个连接池，按部署并发与 DB 连接上限调整
    checkpoint_pool_min_size: int = 1
    checkpoint_pool_max_size: int = 4
    checkpoint_pool_open_timeout_seconds: float = 10.0
    # 审计事实合并窗口：窗口内并发事实共用一次提交；0 表示逐条提交
    audit_batch_window_ms: int = 5
    audit_batch_max_facts: int = 32

    # 后台任务 worker 槽位（ARQ_MAX_JOBS）：与 Run 队列分离，长知识任务不占 AgentRun 容量
    arq_max_jobs: int = 4

    # 会话令牌：Redis 存储 + 滑动续期；过期或 Redis 不可用一律按未认证处理
    session_ttl_seconds: int = 43200
    # 开发态身份请求头（X-Dev-Username）：仅本地开发启用；容器部署显式关闭
    # （deploy/docker-compose.yml 默认 false）
    dev_identity_enabled: bool = True

    lock_master_key: str = DEV_LOCK_MASTER_KEY
    lock_master_key_previous: str = ""
    lock_master_key_active_version: int = 1
    wechat_pay_mchid: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_cert_serial: str = ""
    # 订阅消息模板（支付确认后的密码与入住指引第二通道）：空表示未配置，跳过下发并记事件
    wechat_subscribe_template_id: str = ""

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""
    # P4.3 外发边界：默认按外部托管处理；声明 self-host 才按原样上报。
    # 外发第三方必须显式允许，且强制脱敏标识后再发出（见 langfuse_service）。
    langfuse_self_hosted: bool = False
    langfuse_allow_external_export: bool = False

    # P4.3 可观测与告警：阈值是唯一口径，告警端点只读事实并按阈值判定
    alert_pending_stuck_seconds: int = 300
    alert_running_stuck_seconds: int = 180
    alert_lease_window_minutes: int = 15
    alert_lease_expiry_rate: float = 0.05
    alert_lease_min_attempts: int = 10
    alert_sse_window_minutes: int = 15
    alert_sse_disconnect_rate: float = 0.1
    alert_sse_min_streams: int = 20
    alert_db_pool_usage: float = 0.9
    # SSE 分钟桶保留时长：覆盖最长告警窗口与排障回看
    sse_metrics_ttl_seconds: int = 3600

    # 报表：模型输入单价（每 1K tokens，元），用于估算缓存命中的成本节省；留 0 表示不估算
    report_price_input_per_1k: float = 0.0
    report_price_cached_input_per_1k: float = 0.0

    # 报表预聚合（数据湖 agg_* 分片）开关：默认关闭，报表一律走 PostgreSQL 重算。
    # 陈旧分片会令 token/tool 指标静默归零（remediation-plan §A4 张力），故默认不开快路径。
    report_agg_enabled: bool = False
    # 开启预聚合时的新鲜度上限：分片最后修改时间早于 now - 该秒数即判陈旧 → 回落 PG
    report_agg_max_staleness_seconds: int = 3600

    # 管理员前端跨域来源（部署时设为具体域名，如 ["https://admin.example.com"]）
    cors_origins: list[str] = ["*"]

    # 速率限制配置（0 表示不限流）
    rate_limit_enabled: bool = True
    rate_limit_guest_per_minute: int = 30
    rate_limit_chat_per_minute: int = 20
    rate_limit_admin_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10
    # 已认证请求按会话用户计数（同时保留 IP 层），未认证请求只按 IP 计数
    rate_limit_user_enabled: bool = True
    # 部署级租户总额度 = 分组限额 × 倍数；0 表示不设总额度
    rate_limit_tenant_multiplier: int = 10
    rate_limit_tenant_id: str = "haochen"
    # 仅在可信反向代理（覆盖写入 X-Forwarded-For）之后才信任该头
    rate_limit_trust_forwarded_for: bool = True

    @model_validator(mode="after")
    def _validate_capacity(self) -> "Settings":
        if self.checkpoint_pool_min_size < 1:
            raise ValueError("CHECKPOINT_POOL_MIN_SIZE 至少为 1")
        if self.checkpoint_pool_max_size < self.checkpoint_pool_min_size:
            raise ValueError("CHECKPOINT_POOL_MAX_SIZE 不能小于 CHECKPOINT_POOL_MIN_SIZE")
        if self.checkpoint_pool_open_timeout_seconds <= 0:
            raise ValueError("CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS 必须大于 0")
        if self.message_history_limit < 1:
            raise ValueError("MESSAGE_HISTORY_LIMIT 至少为 1")
        if self.summary_threshold < 1:
            raise ValueError("SUMMARY_THRESHOLD 至少为 1")
        if self.audit_batch_window_ms < 0:
            raise ValueError("AUDIT_BATCH_WINDOW_MS 不能为负")
        if self.audit_batch_max_facts < 1:
            raise ValueError("AUDIT_BATCH_MAX_FACTS 至少为 1")
        return self

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in _PRODUCTION_ENV_NAMES

    def production_issues(self) -> list[str]:
        """生产环境启动前的失败关闭检查：返回不安全项，空列表表示通过。"""
        if not self.is_production:
            return []

        issues: list[str] = []
        if self.database_url == DEV_DATABASE_URL:
            issues.append("DATABASE_URL 仍为开发默认值，必须显式提供生产连接串")
        else:
            try:
                password = make_url(self.database_url).password or ""
            except Exception:
                issues.append("DATABASE_URL 无法解析")
            else:
                if password.lower() in _WEAK_PASSWORDS:
                    issues.append("DATABASE_URL 使用弱口令")

        if self.neo4j_password == DEV_NEO4J_PASSWORD:
            issues.append("NEO4J_PASSWORD 仍为开发默认值")
        elif self.neo4j_password.strip().lower() in _WEAK_PASSWORDS:
            issues.append("NEO4J_PASSWORD 为空或使用弱口令")

        if self.minio_secret_key == DEV_MINIO_SECRET:
            issues.append("MINIO_SECRET_KEY 仍为开发默认值")
        elif self.minio_secret_key.strip().lower() in _WEAK_PASSWORDS:
            issues.append("MINIO_SECRET_KEY 为空或使用弱口令")

        if self.lock_master_key == DEV_LOCK_MASTER_KEY:
            issues.append("LOCK_MASTER_KEY 仍为开发默认值")
        elif len(self.lock_master_key) < 16:
            issues.append("LOCK_MASTER_KEY 长度不足 16 字符")

        if self.device_sim_signing_secret == DEV_DEVICE_SIM_SECRET:
            issues.append("DEVICE_SIM_SIGNING_SECRET 仍为开发默认值")

        if not self.cors_origins or "*" in self.cors_origins:
            issues.append("CORS_ORIGINS 必须显式配置具体域名，不能为空或使用通配符")

        if self.dev_identity_enabled:
            issues.append("DEV_IDENTITY_ENABLED 必须在生产环境关闭")

        if not self.rate_limit_enabled:
            issues.append("RATE_LIMIT_ENABLED 必须在生产环境开启")

        if (
            self.langfuse_public_key
            and self.langfuse_secret_key
            and not self.langfuse_self_hosted
            and not self.langfuse_allow_external_export
        ):
            issues.append(
                "LANGFUSE 已配置密钥但既未声明 LANGFUSE_SELF_HOSTED，"
                "也未显式允许脱敏外发（LANGFUSE_ALLOW_EXTERNAL_EXPORT）"
            )

        return issues

    def validate_production(self) -> None:
        issues = self.production_issues()
        if issues:
            detail = "\n".join(f"- {issue}" for issue in issues)
            raise RuntimeError(f"生产配置不安全，拒绝启动：\n{detail}")

    @property
    def psycopg_dsn(self) -> str:
        """psycopg 连接串（checkpoint 用）：从 database_url 派生，去掉 async 驱动前缀。"""
        return (
            make_url(self.database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
