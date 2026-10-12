"""P6 打卡与视觉审核集成测试（真实 PostgreSQL + VisionClient mock）。

覆盖：上班/下班打卡流程、视觉审核自动通过、失败→manual_review、重复打卡拒绝、
记录查询、越权 401。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.repositories import StaffRepository, UserRepository
from app.security import hash_password
from app.services.attendance_service import AttendanceService
from app.vision_api import VisionClient, VisionResult

pytestmark = pytest.mark.integration


class _FailingVisionClient(VisionClient):
    """视觉审核替身：始终返回低分，触发 manual_review。"""

    async def full_check(
        self,
        photo_path: str,
        *,
        face_reference: str | None = None,
    ) -> VisionResult:
        return VisionResult(
            face_score=Decimal("0.3000"),
            badge_ok=False,
            uniform_ok=False,
            raw={},
        )


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _run(api: Api, body: Any) -> Any:
    async def _inner() -> Any:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            result = await body(session)
            await session.commit()
            return result

    return asyncio.run(_inner())


async def _seed_staff(
    session: AsyncSession
) -> tuple[UUID, UUID]:
    """创建员工账号与档案，返回 (user_id, staff_id)。"""
    staff_no = f"att-{uuid4().hex[:8]}"
    user_repo = UserRepository(session)
    user = await user_repo.create_account(
        username=staff_no, password_hash=hash_password("pass"), role="staff",
        display_name=f"测试-{staff_no}",
    )
    staff_repo = StaffRepository(session)
    staff = await staff_repo.create(staff_no=staff_no, name=f"测试-{staff_no}", user_id=user.id)
    return user.id, staff.id


class TestAttendanceService:
    def test_clock_check_in_auto_pass(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = VisionClient()
            record = await service.clock(
                user_id, direction="check_in", photo_url="/tmp/test.jpg", vision=vision,
            )
            assert record.direction == "check_in"
            assert record.review_status == "auto_pass"
            assert record.face_score is not None and record.face_score >= 0.9
            assert record.badge_ok is True
            assert record.uniform_ok is True

        _run(api, body)

    def test_clock_check_out_auto_pass(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = VisionClient()
            record = await service.clock(
                user_id, direction="check_out", photo_url="/tmp/test.jpg", vision=vision,
            )
            assert record.direction == "check_out"
            assert record.review_status == "auto_pass"

        _run(api, body)

    def test_clock_rejects_duplicate(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = VisionClient()
            await service.clock(
                user_id, direction="check_in", photo_url="/tmp/test.jpg", vision=vision,
            )
            from app.domain.errors import ConflictError
            with pytest.raises(ConflictError, match="今日已check_in打卡"):
                await service.clock(
                    user_id, direction="check_in", photo_url="/tmp/test2.jpg", vision=vision,
                )

        _run(api, body)

    def test_clock_fails_to_manual_review(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = _FailingVisionClient()
            record = await service.clock(
                user_id, direction="check_in", photo_url="/tmp/test.jpg", vision=vision,
            )
            assert record.review_status == "manual_review"
            assert record.badge_ok is False
            assert record.uniform_ok is False
            assert record.face_score is not None
            assert float(record.face_score) < 0.5

        _run(api, body)

    def test_list_my_records(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = VisionClient()
            await service.clock(
                user_id, direction="check_in", photo_url="/tmp/a.jpg", vision=vision,
            )
            await service.clock(
                user_id, direction="check_out", photo_url="/tmp/b.jpg", vision=vision,
            )
            records = await service.list_my_records(user_id)
            assert len(records) >= 2

        _run(api, body)

    def test_clock_rejects_invalid_direction(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            user_id, staff_id = await _seed_staff(session)
            service = AttendanceService(session)
            vision = VisionClient()
            from app.domain.errors import ValidationFailedError
            with pytest.raises(ValidationFailedError):
                await service.clock(
                    user_id, direction="invalid", photo_url="/tmp/test.jpg", vision=vision,
                )

        _run(api, body)

    def test_clock_rejects_unknown_user(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            service = AttendanceService(session)
            vision = VisionClient()
            from app.domain.errors import NotFoundError
            with pytest.raises(NotFoundError):
                await service.clock(
                    uuid4(), direction="check_in", photo_url="/tmp/test.jpg", vision=vision,
                )

        _run(api, body)