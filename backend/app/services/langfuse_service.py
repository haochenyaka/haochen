"""Langfuse 客户端包装：可选组件，未配置密钥时不阻断业务。

不变量：
- 未配置密钥时所有方法为 no-op，不创建客户端实例；
- Run 在模型执行前预创建 trace ID 并固化到 AgentRun.langfuse_trace_id；
- callback 不得改写 langfuse_trace_id（以库中持久化的为准），避免并发乱序；
- 映射：user_id → uid、session_id → thread_id；
- 本地 PostgreSQL 审计是唯一权威事实，Langfuse 只是排障视图。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.config import get_settings

logger = logging.getLogger(__name__)


class LangfuseClient:
    """Langfuse 客户端单例包装。

    全局单例持有 langfuse.Langfuse 实例；客户端创建时自动从配置读取密钥。
    因全局单例在进程生命周期内不变，回调与 Run 执行共享同一实例。
    """

    _instance: LangfuseClient | None = None
    # langfuse 未提供类型存根，客户端持有 Any 以避免逐调用类型断言
    _client: Any = None
    _enabled: bool = False

    def __new__(cls) -> LangfuseClient:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self) -> None:
        settings = get_settings()
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            logger.info("Langfuse 未配置密钥，跳过初始化")
            self._enabled = False
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_base_url or "https://cloud.langfuse.com",
            )
            self._enabled = True
            logger.info("Langfuse 客户端初始化成功")
        except Exception as exc:
            logger.warning("Langfuse 客户端初始化失败，降级为 no-op：%s", exc)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

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
                user_id=str(user_id) if user_id else None,
                session_id=str(thread_id),
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
        try:
            self._client.score(
                trace_id=trace_id,
                name=name,
                value=value,
                comment=comment,
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
