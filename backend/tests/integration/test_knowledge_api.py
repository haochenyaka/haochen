"""知识库管理接口集成测试：上传/读取/重试的角色边界与文档状态守卫。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import get_session
from app.knowledge import upload_service as upload_service_module
from app.main import app
from app.models.knowledge import KbDocument
from app.models.task import Task
from app.repositories import KnowledgeBaseRepository, UserRepository
from app.routers import knowledge_router
from app.security import hash_password
from tests.integration.knowledge_fakes import (
    FakeObjectStorage,
    RecordingTaskDispatcher,
)

pytestmark = pytest.mark.integration

UPLOAD_LIMIT_BYTES = 4096
DOCUMENT_TEXT = "# 早餐说明\n\n早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅。\n"


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine
    dispatcher: RecordingTaskDispatcher


@pytest.fixture
def api(runtime_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(upload_service_module, "ObjectStorage", FakeObjectStorage)
    monkeypatch.setattr(knowledge_router, "ObjectStorage", FakeObjectStorage)
    monkeypatch.setattr(get_settings(), "kb_max_upload_bytes", UPLOAD_LIMIT_BYTES)
    FakeObjectStorage.reset()
    dispatcher = RecordingTaskDispatcher()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[knowledge_router.get_task_dispatcher] = lambda: dispatcher
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine, dispatcher=dispatcher)
    finally:
        app.dependency_overrides.clear()
        FakeObjectStorage.reset()
        asyncio.run(engine.dispose())


def _seed_role(api: Api, *, role: str, password: str = "pass-123456") -> str:
    username = f"{role}-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(password),
                role=role,
                display_name=role,
            )
            await session.commit()

    asyncio.run(_body())
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _create_kb(api: Api, *, code: str | None = None) -> UUID:
    kb_id: UUID | None = None

    async def _body() -> None:
        nonlocal kb_id
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=code or f"kb-{uuid4().hex[:8]}", name="接口测试库", scope="guest"
            )
            await session.commit()
            kb_id = kb.id

    asyncio.run(_body())
    assert kb_id is not None
    return kb_id


def _set_document_status(api: Api, document_id: UUID, *, status: str) -> None:
    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await session.execute(
                update(KbDocument).where(KbDocument.id == document_id).values(status=status)
            )
            await session.commit()

    asyncio.run(_body())


def _set_task_status(api: Api, task_id: UUID, *, status: str) -> None:
    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await session.execute(update(Task).where(Task.id == task_id).values(status=status))
            await session.commit()

    asyncio.run(_body())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(api: Api, token: str, kb_id: UUID, *, content: bytes = DOCUMENT_TEXT.encode()):
    return api.client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents",
        headers=_auth(token),
        files={"file": ("breakfast.md", content, "text/markdown")},
    )


def test_upload_records_parse_task_and_document_is_readable(api: Api) -> None:
    token = _seed_role(api, role="admin")
    kb_id = _create_kb(api)

    response = _upload(api, token, kb_id)
    assert response.status_code == 201, response.text
    body = response.json()
    document_id = UUID(body["document"]["id"])
    assert body["document"]["status"] == "uploaded"
    assert body["document"]["title"] == "breakfast"
    assert body["document"]["size_bytes"] == len(DOCUMENT_TEXT.encode())
    assert api.dispatcher.enqueued == [UUID(body["task_id"])]

    listed = api.client.get(f"/api/admin/knowledge-bases/{kb_id}/documents", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [str(document_id)]

    detail = api.client.get(f"/api/admin/kb-documents/{document_id}", headers=_auth(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["preview"] == []
    assert detail.json()["chunk_count"] == 0

    original = api.client.get(
        f"/api/admin/kb-documents/{document_id}/original", headers=_auth(token)
    )
    assert original.status_code == 200, original.text
    assert original.content == DOCUMENT_TEXT.encode()


def test_knowledge_reads_allow_manager_and_reject_anonymous(api: Api) -> None:
    admin = _seed_role(api, role="admin")
    manager = _seed_role(api, role="manager")
    kb_id = _create_kb(api)

    assert api.client.get("/api/admin/knowledge-bases", headers=_auth(admin)).status_code == 200
    assert api.client.get("/api/admin/knowledge-bases", headers=_auth(manager)).status_code == 200
    assert api.client.get("/api/admin/knowledge-bases").status_code == 401
    assert _upload(api, manager, kb_id).status_code == 403
    assert (
        api.client.get(f"/api/admin/kb-documents/{uuid4()}", headers=_auth(manager)).status_code
        == 404
    )
    assert (
        api.client.get(
            f"/api/admin/knowledge-bases/{uuid4()}/documents", headers=_auth(manager)
        ).status_code
        == 404
    )


def test_upload_rejects_unknown_type_oversize_and_duplicate_content(api: Api) -> None:
    token = _seed_role(api, role="admin")
    kb_id = _create_kb(api)

    unsupported = api.client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents",
        headers=_auth(token),
        files={"file": ("cover.png", b"binary", "image/png")},
    )
    assert unsupported.status_code == 422, unsupported.text

    oversize = _upload(api, token, kb_id, content=b"x" * (UPLOAD_LIMIT_BYTES + 1))
    assert oversize.status_code == 413, oversize.text

    assert _upload(api, token, kb_id).status_code == 201
    duplicate = _upload(api, token, kb_id)
    assert duplicate.status_code == 409, duplicate.text
    assert "相同内容" in duplicate.json()["detail"]


def test_reprocess_follows_document_state_and_deduplicates(api: Api) -> None:
    token = _seed_role(api, role="admin")
    kb_id = _create_kb(api)
    upload = _upload(api, token, kb_id)
    document_id = UUID(upload.json()["document"]["id"])
    parse_task_id = UUID(upload.json()["task_id"])

    pending = api.client.post(
        f"/api/admin/kb-documents/{document_id}/reprocess", headers=_auth(token)
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["task_type"] == "kb.parse"
    assert pending.json()["task_id"] == str(parse_task_id)
    assert pending.json()["deduplicated"] is True

    _set_task_status(api, parse_task_id, status="succeeded")
    fresh = api.client.post(
        f"/api/admin/kb-documents/{document_id}/reprocess", headers=_auth(token)
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["task_type"] == "kb.parse"
    assert fresh.json()["task_id"] != str(parse_task_id)
    assert fresh.json()["deduplicated"] is False

    _set_document_status(api, document_id, status="error_indexing")
    index_retry = api.client.post(
        f"/api/admin/kb-documents/{document_id}/reprocess", headers=_auth(token)
    )
    assert index_retry.status_code == 200, index_retry.text
    assert index_retry.json()["task_type"] == "kb.index"
    assert index_retry.json()["deduplicated"] is False

    _set_document_status(api, document_id, status="indexing")
    busy = api.client.post(f"/api/admin/kb-documents/{document_id}/reprocess", headers=_auth(token))
    assert busy.status_code == 409, busy.text
    assert "正在处理中" in busy.json()["detail"]
