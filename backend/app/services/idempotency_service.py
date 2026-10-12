"""通用幂等组件：(scope, key) 唯一，重复提交返回既有结果而非新建副作用（A3）。

不变量：
- 同一 (scope, key) 至多执行一次副作用：命中既有记录直接回放，不再调用操作体；
- 记录与副作用同事务提交：claim 用 SAVEPOINT 兜住并发唯一冲突，不影响外层事务；
- 幂等域取值为「对象.动作」（如 `order.create`、`lock.rotate`），键由调用方生成。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, ValidationFailedError
from app.models.domain import IdempotencyRecord
from app.repositories.idempotency_repository import IdempotencyRepository

SCOPE_MAX_LENGTH = 64
KEY_MAX_LENGTH = 128


@dataclass(frozen=True)
class IdempotentOutcome:
    """幂等执行结果：`replayed` 为真表示命中既有事实，本次未产生第二次副作用。"""

    result_type: str
    result_ref: UUID
    replayed: bool


Operation = Callable[[], Awaitable[tuple[str, UUID]]]


class IdempotencyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._records = IdempotencyRepository(session)

    async def lookup(self, scope: str, key: str) -> IdempotencyRecord | None:
        _ensure_scope(scope)
        _ensure_key(key)
        return await self._records.get(scope, key)

    async def run(self, scope: str, key: str, operation: Operation) -> IdempotentOutcome:
        """执行副作用：首次调用执行并登记结果，重复调用直接回放既有结果。"""
        existing = await self.lookup(scope, key)
        if existing is not None:
            return _replay(existing)

        claimed = await self._claim(scope, key)
        if claimed is None:
            winner = await self._records.get(scope, key)
            if winner is None:
                raise ConflictError(f"幂等键并发处理中，请稍后重试：{scope}:{key}")
            return _replay(winner)

        result_type, result_ref = await operation()
        await self._records.attach_result(
            claimed.id, result_type=result_type, result_ref=result_ref
        )
        return IdempotentOutcome(result_type=result_type, result_ref=result_ref, replayed=False)

    async def _claim(self, scope: str, key: str) -> IdempotencyRecord | None:
        """SAVEPOINT 内插入占位记录：唯一冲突只回退本段，外层事务仍可继续。"""
        try:
            async with self._session.begin_nested():
                return await self._records.create(scope=scope, key=key)
        except IntegrityError:
            return None


def _replay(record: IdempotencyRecord) -> IdempotentOutcome:
    if record.result_type is None or record.result_ref is None:
        raise ConflictError(f"幂等键已占用且结果未落库，请稍后重试：{record.scope}:{record.key}")
    return IdempotentOutcome(
        result_type=record.result_type, result_ref=record.result_ref, replayed=True
    )


def _ensure_scope(scope: str) -> None:
    if not scope or len(scope) > SCOPE_MAX_LENGTH:
        raise ValidationFailedError(f"幂等域取值非法（1–{SCOPE_MAX_LENGTH} 字符）：{scope!r}")


def _ensure_key(key: str) -> None:
    if not key or len(key) > KEY_MAX_LENGTH:
        raise ValidationFailedError(f"幂等键取值非法（1–{KEY_MAX_LENGTH} 字符）：{key!r}")
