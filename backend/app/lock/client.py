"""门锁渠道客户端：凭证签发/轮换/吊销/预生成/展期与开锁记录（HTTP + JSON，契约 §2）。

不变量：
- 非 2xx 抛 LockError（含 404 未知房间/无凭证、409 版本冲突）；连接/超时抛
  LockUnavailableError（未取得结果），调用方失败关闭：订单不得据此进入 completed；
- 明文 code 只在签发/轮换/预生成响应内流转，调用方即用即弃，禁止持久化与日志（A2）；
- 元数据回读（get_credential / 激活 / 吊销 / 展期）结构上不含 code：真实门锁不提供明文回读；
- 版本按房间单调递增：吊销后重新签发不回退（否则与本地 (room_no, version) 唯一约束冲突）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0


class LockError(RuntimeError):
    """门锁返回明确失败（非 2xx 或响应体不合约）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LockUnavailableError(LockError):
    """门锁不可达：未取得结果，按可重试处理。"""


@dataclass(frozen=True)
class IssuedCredential:
    """签发/轮换结果：`code` 为明文，只允许随下发载荷传递。"""

    credential_id: str
    room_no: str
    code: str
    version: int
    status: str
    reason: str
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class CredentialMetadata:
    credential_id: str
    room_no: str
    version: int
    status: str
    reason: str
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class UnlockRecord:
    record_id: str
    room_no: str
    source: str
    accepted: bool
    at: datetime


class LockClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    @classmethod
    def from_settings(cls) -> LockClient:
        from app.config import get_settings

        return cls(base_url=get_settings().device_sim_base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise LockUnavailableError(f"门锁服务不可达：{path}") from exc
        if response.status_code >= 400:
            raise LockError(
                f"门锁服务返回错误：{path} status={response.status_code}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LockError(f"门锁服务响应不是合法 JSON：{path}") from exc

    async def issue_credential(
        self,
        *,
        room_no: str,
        valid_hours: int | None = None,
        reason: str = "checkin",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> IssuedCredential:
        payload: dict[str, Any] = {"room_no": room_no, "reason": reason}
        if valid_hours is not None:
            payload["valid_hours"] = valid_hours
        _put_window(payload, valid_from=valid_from, valid_to=valid_to)
        body = await self._request("POST", "/lock/v1/credentials", json=payload)
        return _issued_credential(body)

    async def rotate_credential(
        self,
        *,
        room_no: str,
        valid_hours: int | None = None,
        reason: str = "daily",
        expected_version: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> IssuedCredential:
        payload: dict[str, Any] = {"room_no": room_no, "reason": reason}
        if valid_hours is not None:
            payload["valid_hours"] = valid_hours
        if expected_version is not None:
            payload["expected_version"] = expected_version
        _put_window(payload, valid_from=valid_from, valid_to=valid_to)
        body = await self._request("POST", "/lock/v1/credentials/rotate", json=payload)
        return _issued_credential(body)

    async def schedule_next_credential(
        self, *, room_no: str, valid_from: datetime, valid_to: datetime, reason: str = "daily"
    ) -> IssuedCredential:
        """预生成次日凭证（next 槽位）：重复调用整体替换既有 next。"""
        body = await self._request(
            "POST",
            "/lock/v1/credentials/next",
            json={
                "room_no": room_no,
                "reason": reason,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat(),
            },
        )
        return _issued_credential(body)

    async def activate_next_credential(
        self, *, room_no: str, expected_version: int
    ) -> CredentialMetadata:
        """到点激活 next：旧当前凭证被设备置 revoked；不返回明文（无新码产生）。"""
        body = await self._request(
            "POST",
            "/lock/v1/credentials/next/activate",
            json={"room_no": room_no, "expected_version": expected_version},
        )
        return _credential_metadata(body)

    async def revoke_next_credential(
        self, *, room_no: str, expected_version: int | None = None
    ) -> CredentialMetadata:
        payload: dict[str, Any] = {"room_no": room_no}
        if expected_version is not None:
            payload["expected_version"] = expected_version
        body = await self._request("POST", "/lock/v1/credentials/next/revoke", json=payload)
        return _credential_metadata(body)

    async def extend_credential(
        self, *, room_no: str, valid_to: datetime, expected_version: int | None = None
    ) -> CredentialMetadata:
        """展期（续订保持当前凭证）：只改窗口终点，不换码。"""
        payload: dict[str, Any] = {"room_no": room_no, "valid_to": valid_to.isoformat()}
        if expected_version is not None:
            payload["expected_version"] = expected_version
        body = await self._request("POST", "/lock/v1/credentials/extend", json=payload)
        return _credential_metadata(body)

    async def revoke_credential(
        self, *, room_no: str, expected_version: int | None = None
    ) -> CredentialMetadata:
        payload: dict[str, Any] = {"room_no": room_no}
        if expected_version is not None:
            payload["expected_version"] = expected_version
        body = await self._request("POST", "/lock/v1/credentials/revoke", json=payload)
        return _credential_metadata(body)

    async def get_credential(self, room_no: str) -> CredentialMetadata:
        body = await self._request("GET", f"/lock/v1/credentials/{room_no}")
        return _credential_metadata(body)

    async def list_unlock_records(self, *, room_no: str | None = None) -> list[UnlockRecord]:
        path = "/lock/v1/unlock-records"
        if room_no is not None:
            path = f"{path}?room_no={room_no}"
        body = await self._request("GET", path)
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise LockError("开锁记录响应结构不合约")
        return [_unlock_record(item) for item in items]


def _put_window(
    payload: dict[str, Any], *, valid_from: datetime | None, valid_to: datetime | None
) -> None:
    if valid_from is not None:
        payload["valid_from"] = valid_from.isoformat()
    if valid_to is not None:
        payload["valid_to"] = valid_to.isoformat()


def _issued_credential(body: Any) -> IssuedCredential:
    if not isinstance(body, dict):
        raise LockError("门锁凭证响应结构不合约")
    try:
        return IssuedCredential(
            credential_id=str(body["credential_id"]),
            room_no=str(body["room_no"]),
            code=str(body["code"]),
            version=int(body["version"]),
            status=str(body["status"]),
            reason=str(body["reason"]),
            valid_from=datetime.fromisoformat(str(body["valid_from"])),
            valid_to=datetime.fromisoformat(str(body["valid_to"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LockError("门锁凭证响应缺少必需字段或格式不合约") from exc


def _credential_metadata(body: Any) -> CredentialMetadata:
    if not isinstance(body, dict):
        raise LockError("门锁凭证元数据响应结构不合约")
    try:
        return CredentialMetadata(
            credential_id=str(body["credential_id"]),
            room_no=str(body["room_no"]),
            version=int(body["version"]),
            status=str(body["status"]),
            reason=str(body["reason"]),
            valid_from=datetime.fromisoformat(str(body["valid_from"])),
            valid_to=datetime.fromisoformat(str(body["valid_to"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LockError("门锁凭证元数据缺少必需字段或格式不合约") from exc


def _unlock_record(body: Any) -> UnlockRecord:
    if not isinstance(body, dict):
        raise LockError("开锁记录结构不合约")
    try:
        return UnlockRecord(
            record_id=str(body["record_id"]),
            room_no=str(body["room_no"]),
            source=str(body["source"]),
            accepted=bool(body["accepted"]),
            at=datetime.fromisoformat(str(body["at"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LockError("开锁记录缺少必需字段或格式不合约") from exc
