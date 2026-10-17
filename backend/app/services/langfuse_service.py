"""Langfuse 客户端包装：可选组件，未配置密钥时不阻断业务。

不变量：
- 未配置密钥时所有方法为 no-op，不创建客户端实例；
- 只有声明 self-host（LANGFUSE_SELF_HOSTED=true）或显式允许脱敏外发
  （LANGFUSE_ALLOW_EXTERNAL_EXPORT=true）时才初始化客户端；默认拒绝外发；
- 外部托管模式下，用户/会话标识一律 HMAC 哈希后再上报，自由文本（score comment）
  一律丢弃；self-host 模式按原值上报，便于内部排障；
- Run 在模型执行前预创建 trace ID 并固化到 AgentRun.langfuse_trace_id；
- callback 不得改写 langfuse_trace_id（以库中持久化的为准），避免并发乱序；
- 映射：user_id → uid、session_id → thread_id；
- 本地 PostgreSQL 审计是唯一权威事实，Langfuse 只是排障视图。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import UUID

from app.config import get_settings

logger = logging.getLogger(__name__)

CLOUD_LANGFUSE_HOST = "https://cloud.langfuse.com"
MASKED_PREFIX = "masked_"
MASKED_DIGEST_CHARS = 32

EXPORT_DISABLED = "disabled"
EXPORT_SELF_HOSTED = "self_hosted"
EXPORT_MASKED_EXTERNAL = "masked_external"


def mask_identifier(value: str | None, *, salt: str) -> str | None:
    """稳定脱敏：同一标识在同一部署内可关联，但不可逆推出原值。"""
    if not value:
        return None
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
    return f"{MASKED_PREFIX}{digest[:MASKED_DIGEST_CHARS]}"


class LangfuseClient:
    """Langfuse 客户端单例包装。

    全局单例持有 langfuse.Langfuse 实例；客户端创建时自动从配置读取密钥。
    因全局单例在进程生命周期内不变，回调与 Run 执行共享同一实例。
    """

    _instance: LangfuseClient | None = None
    # langfuse 未提供类型存根，客户端持有 Any 以避免逐调用类型断言
    _client: Any = None
    _enabled: bool = False
    _external: bool = True
    _export_mode: str = EXPORT_DISABLED
    _masking_salt: str = ""

    def __new__(cls) -> LangfuseClient:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self) -> None:
        settings = get_settings()
        self._masking_salt = settings.lock_master_key
        self._external = not settings.langfuse_self_hosted
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            logger.info("Langfuse 未配置密钥，跳过初始化")
            self._enabled = False
            self._export_mode = EXPORT_DISABLED
            return
        if self._external and not settings.langfuse_allow_external_export:
            # 失败关闭：未声明 self-host 也未允许外发时，不向任何第三方发送标识
            logger.warning(
                "Langfuse 按外部托管处理且未显式允许脱敏外发，跳过初始化（本地审计不受影响）"
            )
            self._enabled = False
            self._export_mode = EXPORT_DISABLED
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_base_url or CLOUD_LANGFUSE_HOST,
            )
            self._enabled = True
            self._export_mode = EXPORT_MASKED_EXTERNAL if self._external else EXPORT_SELF_HOSTED
            logger.info("Langfuse 客户端初始化成功：export_mode=%s", self._export_mode)
        except Exception as exc:
            logger.warning("Langfuse 客户端初始化失败，降级为 no-op：%s", exc)
            self._enabled = False
            self._export_mode = EXPORT_DISABLED

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def export_mode(self) -> str:
        return self._export_mode

    def _identifier(self, value: str | None) -> str | None:
        """外部托管时脱敏标识；self-host 保留原值。"""
        if not self._external:
            return value
        return mask_identifier(value, salt=self._masking_salt)

    def create_trace(
        self,
        *,
        trace_id: str,
        run_id: UUID,
        thread_id: UUID,
        user_id: UUID | None,
        agent_slug: str,
        request_id: UUID | None = None,
    ) -> str | None:
        """预创建 trace 并固化 ID。

        返回 trace_id（传入值），调用方写入 AgentRun.langfuse_trace_id。
        未启用时返回 None。
        """
        if not self._enabled or self._client is None:
            return None
        try:
            self._client.trace(
                id=trace_id,
                name=f"agent_run_{agent_slug}",
                user_id=self._identifier(str(user_id) if user_id else None),
                session_id=self._identifier(str(thread_id)),
                # 元数据只允许白名单键，避免调用方误传自由文本或 PII
                metadata={
                    "run_id": str(run_id),
                    "request_id": str(request_id) if request_id else None,
                    "agent_slug": agent_slug,
                },
            )
            return trace_id
        except Exception as exc:
            logger.warning("Langfuse trace 创建失败：%s", exc)
            return None

    def score_feedback(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """将点赞/点踩等反馈写入 Langfuse score。

        value: 1.0（点赞/有帮助）, 0.0（点踩/无帮助）
        """
        if not self._enabled or self._client is None:
            return
        # 外部托管模式不发送自由文本：评分备注可能包含客人原话
        safe_comment = None if self._external else comment
        try:
            self._client.score(
                trace_id=trace_id,
                name=name,
                value=value,
                comment=safe_comment,
            )
        except Exception as exc:
            logger.warning("Langfuse score 写入失败：%s", exc)

    def flush(self) -> None:
        """显式刷新缓冲区（可选）。"""
        if self._enabled and self._client is not None:
            try:
                self._client.flush()
            except Exception:
                pass


def get_langfuse() -> LangfuseClient:
    """获取全局 LangfuseClient 单例。"""
    return LangfuseClient()
