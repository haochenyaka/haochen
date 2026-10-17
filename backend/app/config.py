from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "haochen-server"
    app_version: str = "0.1.0"
    env: str = "local"

    database_url: str = "postgresql+asyncpg://haochen:haochen@localhost:5432/haochen"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "haochen"
    minio_secret_key: str = "haochen-dev-secret"
    minio_secure: bool = False
    minio_bucket_knowledge: str = "haochen-knowledge"

    milvus_uri: str = "http://localhost:19530"
    milvus_collection_prefix: str = "haochen_kb"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "haochen-dev-secret"
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

    # P1.4 长期记忆写入：Run 终态后按策略抽取候选记忆写 GuestMemory；默认关（LLM 成本）
    memory_extract_enabled: bool = False
    memory_extract_max_items: int = 5
    memory_extract_min_chars: int = 4
    memory_extract_max_chars: int = 200

    device_sim_base_url: str = "http://localhost:9100"
    device_sim_signing_secret: str = "dev-signing-secret"

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
    summary_threshold: int = 100_000
    summary_keep_messages: int = 10
    large_result_threshold_bytes: int = 8192
    large_result_preview_chars: int = 200
    model_retry_max_attempts: int = 3
    model_retry_backoff_seconds: float = 0.5

    # 运行事件（SSE）：Redis Stream 承载投影事件，TTL 与长度是保留上限
    run_event_ttl_seconds: int = 3600
    run_event_maxlen: int = 512
    sse_heartbeat_seconds: float = 15.0
    sse_follow_block_ms: int = 2000

    # 后台任务 worker 槽位（ARQ_MAX_JOBS）：与 Run 队列分离，长知识任务不占 AgentRun 容量
    arq_max_jobs: int = 4

    # 会话令牌：Redis 存储 + 滑动续期；过期或 Redis 不可用一律按未认证处理
    session_ttl_seconds: int = 43200
    # 开发态身份请求头（X-Dev-Username）：仅本地开发启用；容器部署显式关闭
    # （deploy/docker-compose.yml 默认 false）
    dev_identity_enabled: bool = True

    lock_master_key: str = "change-me"
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

    # 报表：模型输入单价（每 1K tokens，元），用于估算缓存命中的成本节省；留 0 表示不估算
    report_price_input_per_1k: float = 0.0
    report_price_cached_input_per_1k: float = 0.0

    # 管理员前端跨域来源（部署时设为具体域名，如 ["https://admin.example.com"]）
    cors_origins: list[str] = ["*"]

    # 速率限制配置（0 表示不限流）
    rate_limit_enabled: bool = True
    rate_limit_guest_per_minute: int = 30
    rate_limit_chat_per_minute: int = 20
    rate_limit_admin_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10

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
