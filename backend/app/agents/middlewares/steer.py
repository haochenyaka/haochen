"""工位 1 Steer：安全边界发现待接替请求（新消息取代当前 Run 的模型调用）。

接管语义：启用后，模型调用前检查线程是否已排入更新的用户请求；有则置取消信号并中断
本 Run，新请求经既有取消/接力链路排队并派发。用进程内取消信号 + 会话按需查库，不引入
Run 终结或检查点查询，避免额外依赖。

启用方式：**配置驱动、默认关闭**——由 Agent 配置的 `middleware.steer.enabled` 打开
（经 PR-0.1 的配置链路）；接线只提供会话来源 / 线程 / 取消信号，不在装配期隐式改变
Run 行为（默认流的"把当前问完再答下一条"语义保持不变）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.middlewares.base import (
    MiddlewareConfigError,
    ModelCallRequest,
    Station,
    SteerSuperseded,
)
from app.repositories import RequestRepository

SessionFactory = async_sessionmaker[AsyncSession]
CancelSignal = Callable[[], None]


@dataclass(frozen=True)
class SteerSettings:
    enabled: bool = False


class SteerStation(Station):
    key = "steer"
    label = "Steer 安全边界"

    def __init__(
        self,
        *,
        settings: SteerSettings | None = None,
        session_factory: SessionFactory | None = None,
        thread_id: UUID | None = None,
        cancel_signal: CancelSignal | None = None,
    ) -> None:
        resolved = settings or SteerSettings()
        # 配置驱动、默认关闭：接线只提供查库与取消能力，不在装配期隐式改变 Run 行为
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        wired = session_factory is not None and thread_id is not None and cancel_signal is not None
        if self.enabled and not wired:
            raise MiddlewareConfigError(
                "Steer 已启用但缺少会话来源 / 线程 / 取消信号（由装配期注入）"
            )
        self._session_factory = session_factory
        self._thread_id = thread_id
        self._cancel_signal = cancel_signal
        self._superseded: dict[str, Any] | None = None

    @property
    def superseded(self) -> dict[str, Any] | None:
        """被接管的证据（线程、待接管请求 id、排队位置）；未被接管时为 None。"""
        return self._superseded

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        if self._session_factory is None or self._thread_id is None or self._cancel_signal is None:
            return request
        async with self._session_factory() as session:
            repository = RequestRepository(session)
            head = await repository.get_head_queued(self._thread_id)
            position = await repository.queued_position(head.id) if head is not None else None
        if head is None:
            return request
        self._cancel_signal()
        self._superseded = {
            "thread_id": str(self._thread_id),
            "request_id": str(head.id),
            "queue_position": position,
        }
        raise SteerSuperseded(f"线程已排入更新的请求：{head.id}")
