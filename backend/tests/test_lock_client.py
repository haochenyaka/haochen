"""门锁渠道客户端 ↔ 设备模拟器契约测试（ASGI 直连真实 mock）：防止客户端与契约 §2 漂移。

覆盖 P4-T7 新增的 next 槽位与展期：预生成 → 到点激活 → 展期保持版本；
以及版本单调（吊销后重新签发不回退，本地 (room_no, version) 唯一约束依赖该性质）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.device_sim.main import app as sim_app
from app.lock.client import LockClient, LockError

BASE_URL = "http://lock.test"


def _client() -> tuple[LockClient, httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=sim_app)
    client = LockClient(base_url=BASE_URL)
    client._client = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
    control = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
    return client, control


def test_next_slot_promotion_extend_and_version_monotonic() -> None:
    async def _run() -> None:
        client, control = _client()
        try:
            await control.post("/admin/v1/reset")
            issued = await client.issue_credential(room_no="8401", valid_hours=24)
            assert issued.version == 1
            assert issued.code.isdigit() and len(issued.code) == 6

            start = datetime.now(UTC) + timedelta(hours=1)
            scheduled = await client.schedule_next_credential(
                room_no="8401", valid_from=start, valid_to=start + timedelta(hours=24)
            )
            assert scheduled.status == "next"
            assert scheduled.version == issued.version + 1

            promoted = await client.activate_next_credential(
                room_no="8401", expected_version=scheduled.version
            )
            assert promoted.status == "active"
            assert promoted.credential_id == scheduled.credential_id
            assert promoted.valid_from == scheduled.valid_from

            extended = await client.extend_credential(
                room_no="8401",
                valid_to=start + timedelta(days=2),
                expected_version=promoted.version,
            )
            assert extended.version == promoted.version
            assert extended.valid_to == start + timedelta(days=2)

            with pytest.raises(LockError) as empty_slot:
                await client.revoke_next_credential(room_no="8401")
            assert empty_slot.value.status_code == 404

            revoked = await client.revoke_credential(room_no="8401")
            assert revoked.status == "revoked"
            reissued = await client.issue_credential(room_no="8401", valid_hours=24)
            assert reissued.version == promoted.version + 1
        finally:
            await client.aclose()
            await control.aclose()

    asyncio.run(_run())


def test_version_conflicts_surface_as_lock_error() -> None:
    async def _run() -> None:
        client, control = _client()
        try:
            await control.post("/admin/v1/reset")
            issued = await client.issue_credential(room_no="8402", valid_hours=24)
            start = datetime.now(UTC) + timedelta(hours=1)
            scheduled = await client.schedule_next_credential(
                room_no="8402", valid_from=start, valid_to=start + timedelta(hours=24)
            )
            with pytest.raises(LockError) as stale:
                await client.activate_next_credential(
                    room_no="8402", expected_version=issued.version
                )
            assert stale.value.status_code == 409

            with pytest.raises(LockError) as stale_revoke:
                await client.revoke_next_credential(
                    room_no="8402", expected_version=scheduled.version + 1
                )
            assert stale_revoke.value.status_code == 409

            cleared = await client.revoke_next_credential(
                room_no="8402", expected_version=scheduled.version
            )
            assert cleared.status == "revoked"
        finally:
            await client.aclose()
            await control.aclose()

    asyncio.run(_run())
