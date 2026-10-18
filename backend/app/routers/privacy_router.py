"""隐私告知与同意记录 API。

- `GET /api/privacy/notice` 公开：告知版本、用途、数据类别、保留期与出境口径；
- `GET /api/privacy/consents` 需登录：返回本人各用途的当前状态；
- `POST /api/privacy/consents` 需登录：记录同意或撤回（追加事件）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_identity
from app.services.consent_service import (
    CONSENT_PURPOSES,
    ConsentPurpose,
    ConsentService,
    StalePolicyVersion,
    UnknownConsentPurpose,
    build_notice,
)

router = APIRouter(prefix="/privacy", tags=["privacy"])


class PurposeNotice(BaseModel):
    purpose: str
    title: str
    required: bool
    description: str


class NoticeResponse(BaseModel):
    policy_version: str
    purposes: list[PurposeNotice]
    data_categories: list[str]
    retention: list[str]
    cross_border: str


class ConsentItem(BaseModel):
    purpose: str
    title: str
    required: bool
    granted: bool
    policy_version: str | None
    updated_at: datetime | None


class ConsentListResponse(BaseModel):
    policy_version: str
    items: list[ConsentItem]


class ConsentBody(BaseModel):
    purpose: str = Field(min_length=1, max_length=32)
    granted: bool
    policy_version: str | None = Field(default=None, max_length=32)
    channel: str = Field(default="api", min_length=1, max_length=16)


def _item(
    purpose: ConsentPurpose,
    record_granted: bool,
    version: str | None,
    updated_at: datetime | None,
) -> ConsentItem:
    return ConsentItem(
        purpose=purpose.purpose,
        title=purpose.title,
        required=purpose.required,
        granted=record_granted,
        policy_version=version,
        updated_at=updated_at,
    )


@router.get("/notice", response_model=NoticeResponse)
async def get_privacy_notice() -> NoticeResponse:
    notice = build_notice()
    return NoticeResponse(
        policy_version=notice.policy_version,
        purposes=[
            PurposeNotice(
                purpose=item.purpose,
                title=item.title,
                required=item.required,
                description=item.description,
            )
            for item in notice.purposes
        ],
        data_categories=list(notice.data_categories),
        retention=list(notice.retention),
        cross_border=notice.cross_border,
    )


@router.get("/consents", response_model=ConsentListResponse)
async def list_consents(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> ConsentListResponse:
    service = ConsentService(session)
    current = await service.current(identity.user_id)
    items = []
    for purpose in CONSENT_PURPOSES:
        record = current.get(purpose.purpose)
        items.append(
            _item(
                purpose,
                record.granted if record is not None else False,
                record.policy_version if record is not None else None,
                record.created_at if record is not None else None,
            )
        )
    return ConsentListResponse(policy_version=service.policy_version, items=items)


@router.post(
    "/consents",
    response_model=ConsentItem,
    status_code=status.HTTP_201_CREATED,
)
async def record_consent(
    body: ConsentBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> ConsentItem:
    service = ConsentService(session)
    try:
        record = await service.record(
            user_id=identity.user_id,
            purpose=body.purpose,
            granted=body.granted,
            policy_version=body.policy_version,
            channel=body.channel,
        )
    except UnknownConsentPurpose as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except StalePolicyVersion as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()
    await session.refresh(record)
    purpose = next(item for item in CONSENT_PURPOSES if item.purpose == record.purpose)
    return _item(purpose, record.granted, record.policy_version, record.created_at)
