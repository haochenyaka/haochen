"""工位 8 ModelRetry：按配置重试模型调用。

模型层 max_retries=0，重试统一由本工位承担：每次尝试都是一次真实调用，成功与失败的
尝试各自写审计事实。取消（CancelledError 非 Exception 子类）不重试。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import AIMessage

from app.agents.middlewares.base import ModelCallDelegate, ModelCallRequest, Station

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ModelRetrySettings:
    enabled: bool = True
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0


class ModelRetryStation(Station):
    key = "model_retry"
    label = "ModelRetry 模型重试"

    def __init__(
        self, *, settings: ModelRetrySettings | None = None, sleeper: Sleeper | None = None
    ) -> None:
        resolved = settings or ModelRetrySettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._sleeper = sleeper or asyncio.sleep

    async def call_model(self, request: ModelCallRequest, call: ModelCallDelegate) -> AIMessage:
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                return await call()
            except Exception as exc:
                if attempt >= self._settings.max_attempts:
                    raise
                delay = self._backoff(attempt)
                logger.warning(
                    "模型调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                    attempt,
                    self._settings.max_attempts,
                    delay,
                    exc,
                )
                await self._sleeper(delay)
        raise AssertionError("重试循环不可达")

    def _backoff(self, attempt: int) -> float:
        delay = self._settings.initial_backoff_seconds * (
            self._settings.backoff_multiplier ** (attempt - 1)
        )
        return min(delay, self._settings.max_backoff_seconds)
