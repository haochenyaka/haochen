"""通用幂等组件单元测试：重放判定、占位冲突与参数校验（不触库）。"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.errors import ConflictError, ValidationFailedError
from app.models.domain import IdempotencyRecord
from app.services.idempotency_service import IdempotencyService


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeSession:
    def __init__(self) -> None:
        self.savepoints = 0

    def begin_nested(self) -> _Savepoint:
        self.savepoints += 1
        return _Savepoint()


class FakeRecords:
    """脚本化仓储替身：按顺序返回 get 结果，create 可配置为唯一冲突。"""

    def __init__(
        self,
        *,
        responses: list[IdempotencyRecord | None] | None = None,
        conflict: bool = False,
    ) -> None:
        self._responses = list(responses or [])
        self._conflict = conflict
        self.created: list[tuple[str, str]] = []
        self.attached: list[tuple[UUID, str, UUID]] = []

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        return self._responses.pop(0) if self._responses else None

    async def create(self, *, scope: str, key: str) -> IdempotencyRecord:
        if self._conflict:
            raise IntegrityError("insert idempotency_records", {}, Exception("duplicate key"))
        self.created.append((scope, key))
        return IdempotencyRecord(id=uuid4(), scope=scope, key=key)

    async def attach_result(self, record_id: UUID, *, result_type: str, result_ref: UUID) -> bool:
        self.attached.append((record_id, result_type, result_ref))
        return True


def _record(*, result_ref: UUID | None) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=uuid4(),
        scope="order.create",
        key="O-1",
        result_type="order" if result_ref is not None else None,
        result_ref=result_ref,
    )


def _service(records: FakeRecords) -> tuple[IdempotencyService, FakeSession]:
    session = FakeSession()
    service = IdempotencyService(session)  # type: ignore[arg-type]
    service._records = records  # type: ignore[assignment]
    return service, session


async def _forbidden() -> tuple[str, UUID]:
    raise AssertionError("本次调用不应执行副作用")


def test_run_executes_and_attaches_result_once() -> None:
    records = FakeRecords()
    service, session = _service(records)
    calls: list[str] = []
    ref = uuid4()

    async def operation() -> tuple[str, UUID]:
        calls.append("called")
        return ("order", ref)

    outcome = asyncio.run(service.run("order.create", "O-1", operation))

    assert outcome.replayed is False
    assert outcome.result_type == "order"
    assert outcome.result_ref == ref
    assert calls == ["called"]
    assert records.created == [("order.create", "O-1")]
    assert len(records.attached) == 1
    assert records.attached[0][1:] == ("order", ref)
    assert session.savepoints == 1


def test_replay_skips_operation() -> None:
    ref = uuid4()
    records = FakeRecords(responses=[_record(result_ref=ref)])
    service, session = _service(records)

    outcome = asyncio.run(service.run("order.create", "O-1", _forbidden))

    assert outcome.replayed is True
    assert outcome.result_ref == ref
    assert records.created == []
    assert session.savepoints == 0


def test_claim_without_result_conflicts() -> None:
    records = FakeRecords(responses=[_record(result_ref=None)])
    service, _ = _service(records)

    with pytest.raises(ConflictError, match="结果未落库"):
        asyncio.run(service.run("order.create", "O-1", _forbidden))


def test_lost_claim_race_replays_winner() -> None:
    ref = uuid4()
    records = FakeRecords(responses=[None, _record(result_ref=ref)], conflict=True)
    service, session = _service(records)

    outcome = asyncio.run(service.run("order.create", "O-1", _forbidden))

    assert outcome.replayed is True
    assert outcome.result_ref == ref
    assert session.savepoints == 1


def test_lost_claim_race_without_visible_winner_conflicts() -> None:
    records = FakeRecords(responses=[None, None], conflict=True)
    service, _ = _service(records)

    with pytest.raises(ConflictError, match="并发处理中"):
        asyncio.run(service.run("order.create", "O-1", _forbidden))


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        ("", "O-1"),
        ("x" * 65, "O-1"),
        ("order.create", ""),
        ("order.create", "x" * 129),
    ],
)
def test_invalid_scope_or_key_rejected_before_touching_storage(scope: str, key: str) -> None:
    records = FakeRecords()
    service, session = _service(records)

    with pytest.raises(ValidationFailedError):
        asyncio.run(service.run(scope, key, _forbidden))
    assert records.created == []
    assert session.savepoints == 0
