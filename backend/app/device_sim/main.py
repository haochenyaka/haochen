import asyncio
import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="HaoChen Device Simulator", version="0.1.0")

SIGNING_SECRET = os.getenv("DEVICE_SIM_SIGNING_SECRET", "dev-signing-secret")

_lock = asyncio.Lock()
_credentials: dict[str, dict[str, Any]] = {}
_next_credentials: dict[str, dict[str, Any]] = {}
_versions: dict[str, int] = {}
_credential_history: dict[str, list[dict[str, Any]]] = {}
_unlock_records: list[dict[str, Any]] = []
_deliveries: dict[str, dict[str, Any]] = {}
_payments: dict[str, dict[str, Any]] = {}
_notifications: list[dict[str, Any]] = []

# system -> 故障配置；system 取路径第一段（lock / robot / wechat）
_faults: dict[str, dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# 故障注入：error（错误码）、timeout（延迟）、malformed（非法响应体）
# ---------------------------------------------------------------------------


class FaultConfig(BaseModel):
    system: Literal["lock", "robot", "wechat"]
    mode: Literal["none", "error", "timeout", "malformed"] = "error"
    status_code: int = Field(default=503, ge=400, le=599)
    sleep_seconds: float = Field(default=30.0, ge=0)
    remaining: int | None = Field(default=None, ge=1, description="生效次数；空表示持续生效")


@app.middleware("http")
async def fault_injection(request: Request, call_next: Any) -> Response:
    system = request.url.path.strip("/").split("/")[0]
    fault = _faults.get(system)
    if fault and fault["mode"] != "none":
        if fault["remaining"] is None or fault["remaining"] > 0:
            if fault["remaining"] is not None:
                fault["remaining"] -= 1
            if fault["mode"] == "error":
                return JSONResponse(
                    status_code=fault["status_code"],
                    content={"error": "injected_fault", "system": system},
                )
            if fault["mode"] == "timeout":
                await asyncio.sleep(fault["sleep_seconds"])
            if fault["mode"] == "malformed":
                return Response(
                    content=b"{malformed", media_type="application/json", status_code=200
                )
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/v1/reset")
async def reset_state() -> dict[str, str]:
    async with _lock:
        _credentials.clear()
        _next_credentials.clear()
        _versions.clear()
        _credential_history.clear()
        _unlock_records.clear()
        _deliveries.clear()
        _payments.clear()
        _notifications.clear()
        _faults.clear()
    return {"status": "reset"}


@app.get("/faults/v1/config")
async def get_faults() -> dict[str, Any]:
    return {"faults": _faults}


@app.post("/faults/v1/config")
async def configure_fault(payload: FaultConfig) -> dict[str, Any]:
    _faults[payload.system] = payload.model_dump()
    return {"faults": _faults}


@app.delete("/faults/v1/config")
async def clear_faults() -> dict[str, Any]:
    _faults.clear()
    return {"faults": _faults}


# ---------------------------------------------------------------------------
# 门锁：凭证签发 / 轮换 / 吊销 / 预生成（next）/ 展期 / 开锁校验
# 明文只在设备侧保存与校验，符合"数据库只存哈希、明文只存在于设备与下发载荷"的边界。
# 版本按房间单调递增（含吊销后重新签发，不回退）；`next` 槽位承载次日预生成凭证，
# 激活时整体替换当前凭证——当日凭证随窗口到期作废（契约 §2.1）。
# ---------------------------------------------------------------------------


DEFAULT_VALID_HOURS = 24


def _next_version(room_no: str) -> int:
    version = _versions.get(room_no, 0) + 1
    _versions[room_no] = version
    return version


def _validate_window(*, valid_from: datetime, valid_to: datetime) -> None:
    if valid_to <= valid_from:
        raise HTTPException(status_code=422, detail="invalid_validity_window")


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    """元数据视图：结构上不含明文（对齐真实门锁的不可回读）。"""
    return {key: value for key, value in record.items() if key != "code"}


def _retire_previous(room_no: str, record: dict[str, Any]) -> None:
    record["status"] = "revoked"
    _credential_history.setdefault(room_no, []).append(record)


def _issue_credential(
    room_no: str,
    reason: str,
    *,
    valid_hours: int | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> dict[str, Any]:
    previous = _credentials.get(room_no)
    if previous is not None:
        _retire_previous(room_no, previous)

    start = valid_from or _now()
    end = valid_to or start + timedelta(hours=valid_hours or DEFAULT_VALID_HOURS)
    record: dict[str, Any] = {
        "credential_id": f"cred_{secrets.token_hex(8)}",
        "room_no": room_no,
        "code": _generate_code(),
        "version": _next_version(room_no),
        "status": "active",
        "reason": reason,
        "valid_from": start.isoformat(),
        "valid_to": end.isoformat(),
    }
    _credentials[room_no] = record
    return dict(record)


def _schedule_next(
    room_no: str, reason: str, *, valid_from: datetime, valid_to: datetime
) -> dict[str, Any]:
    """预生成次日凭证：重复调用整体替换（旧 next 置 revoked 入历史），版本继续递增。"""
    _validate_window(valid_from=valid_from, valid_to=valid_to)
    previous = _next_credentials.get(room_no)
    if previous is not None:
        _retire_previous(room_no, previous)
    record: dict[str, Any] = {
        "credential_id": f"cred_{secrets.token_hex(8)}",
        "room_no": room_no,
        "code": _generate_code(),
        "version": _next_version(room_no),
        "status": "next",
        "reason": reason,
        "valid_from": valid_from.isoformat(),
        "valid_to": valid_to.isoformat(),
    }
    _next_credentials[room_no] = record
    return dict(record)


def _in_window(record: dict[str, Any]) -> bool:
    now = _now()
    return (
        datetime.fromisoformat(record["valid_from"])
        <= now
        < datetime.fromisoformat(record["valid_to"])
    )


class IssueCredentialRequest(BaseModel):
    room_no: str
    valid_hours: int = Field(default=24, gt=0)
    reason: Literal["checkin", "daily", "renewal"] = "checkin"
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class RotateCredentialRequest(BaseModel):
    room_no: str
    valid_hours: int = Field(default=24, gt=0)
    reason: Literal["daily", "checkout", "leak", "delivery"] = "daily"
    # 契约 §2.3：携带期望版本防旧消息重放改密；缺省不校验（真实门锁协议待接入，见 §7）
    expected_version: int | None = Field(default=None, ge=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class ScheduleNextRequest(BaseModel):
    room_no: str
    reason: Literal["daily"] = "daily"
    valid_from: datetime
    valid_to: datetime


class RoomRequest(BaseModel):
    room_no: str
    expected_version: int | None = Field(default=None, ge=1)


class ExtendCredentialRequest(BaseModel):
    room_no: str
    valid_to: datetime
    expected_version: int | None = Field(default=None, ge=1)


class UnlockRequest(BaseModel):
    room_no: str
    code: str
    source: Literal["guest_app", "front_desk", "housekeeping"] = "guest_app"


@app.post("/lock/v1/credentials")
async def issue_credential(payload: IssueCredentialRequest) -> dict[str, Any]:
    async with _lock:
        return _issue_credential(
            payload.room_no,
            payload.reason,
            valid_hours=payload.valid_hours,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )


@app.post("/lock/v1/credentials/rotate")
async def rotate_credential(payload: RotateCredentialRequest) -> dict[str, Any]:
    async with _lock:
        current = _credentials.get(payload.room_no)
        if (
            current is not None
            and payload.expected_version is not None
            and current["version"] != payload.expected_version
        ):
            raise HTTPException(status_code=409, detail="version_conflict")
        return _issue_credential(
            payload.room_no,
            payload.reason,
            valid_hours=payload.valid_hours,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )


@app.post("/lock/v1/credentials/revoke")
async def revoke_credential(payload: RoomRequest) -> dict[str, Any]:
    async with _lock:
        current = _credentials.get(payload.room_no)
        if (
            current is not None
            and payload.expected_version is not None
            and current["version"] != payload.expected_version
        ):
            raise HTTPException(status_code=409, detail="version_conflict")
        record = _credentials.pop(payload.room_no, None)
        if record is None:
            raise HTTPException(status_code=404, detail="credential_not_found")
        _retire_previous(payload.room_no, record)
        return _metadata(record)


@app.post("/lock/v1/credentials/next")
async def schedule_next_credential(payload: ScheduleNextRequest) -> dict[str, Any]:
    async with _lock:
        return _schedule_next(
            payload.room_no,
            payload.reason,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )


@app.post("/lock/v1/credentials/next/activate")
async def activate_next_credential(payload: RoomRequest) -> dict[str, Any]:
    """到点激活：next 转为当前凭证，旧当前凭证立即 revoked；返回元数据（无明文）。"""
    async with _lock:
        record = _next_credentials.get(payload.room_no)
        if record is None:
            raise HTTPException(status_code=404, detail="next_credential_not_found")
        if payload.expected_version is not None and record["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="version_conflict")
        previous = _credentials.get(payload.room_no)
        if previous is not None:
            _retire_previous(payload.room_no, previous)
        record["status"] = "active"
        _credentials[payload.room_no] = record
        del _next_credentials[payload.room_no]
        return _metadata(record)


@app.post("/lock/v1/credentials/next/revoke")
async def revoke_next_credential(payload: RoomRequest) -> dict[str, Any]:
    async with _lock:
        record = _next_credentials.get(payload.room_no)
        if record is None:
            raise HTTPException(status_code=404, detail="next_credential_not_found")
        if payload.expected_version is not None and record["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="version_conflict")
        _retire_previous(payload.room_no, record)
        del _next_credentials[payload.room_no]
        return _metadata(record)


@app.post("/lock/v1/credentials/extend")
async def extend_credential(payload: ExtendCredentialRequest) -> dict[str, Any]:
    """展期（续订保持当前凭证）：只改生效窗口终点，不换码、不换版本。"""
    async with _lock:
        record = _credentials.get(payload.room_no)
        if record is None:
            raise HTTPException(status_code=404, detail="credential_not_found")
        if payload.expected_version is not None and record["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="version_conflict")
        _validate_window(
            valid_from=datetime.fromisoformat(record["valid_from"]), valid_to=payload.valid_to
        )
        record["valid_to"] = payload.valid_to.isoformat()
        return _metadata(record)


@app.get("/lock/v1/credentials/{room_no}")
async def get_credential(room_no: str) -> dict[str, Any]:
    record = _credentials.get(room_no)
    if record is None:
        raise HTTPException(status_code=404, detail="credential_not_found")
    return record


@app.get("/lock/v1/credentials/{room_no}/next")
async def get_next_credential(room_no: str) -> dict[str, Any]:
    record = _next_credentials.get(room_no)
    if record is None:
        raise HTTPException(status_code=404, detail="next_credential_not_found")
    return record


@app.post("/lock/v1/unlock")
async def unlock(payload: UnlockRequest) -> dict[str, Any]:
    record = _credentials.get(payload.room_no)
    accepted = bool(
        record is not None
        and record["status"] == "active"
        and record["code"] == payload.code
        and _in_window(record)
    )
    entry = {
        "record_id": f"unlock_{secrets.token_hex(6)}",
        "room_no": payload.room_no,
        "source": payload.source,
        "accepted": accepted,
        "at": _now().isoformat(),
    }
    _unlock_records.append(entry)
    return entry


@app.get("/lock/v1/unlock-records")
async def list_unlock_records(room_no: str | None = None) -> dict[str, Any]:
    items = [item for item in _unlock_records if room_no is None or item["room_no"] == room_no]
    return {"items": items[-100:]}


# ---------------------------------------------------------------------------
# 机器人配送：queued → dispatching → delivering → arrived → delivered / failed
# ---------------------------------------------------------------------------


class DeliveryRequest(BaseModel):
    order_no: str
    room_no: str
    item: str


class AdvanceDeliveryRequest(BaseModel):
    status: Literal["dispatching", "delivering", "arrived", "delivered", "failed"]


@app.post("/robot/v1/deliveries")
async def create_delivery(payload: DeliveryRequest) -> dict[str, Any]:
    delivery = {
        "delivery_id": f"dev_{secrets.token_hex(6)}",
        "order_no": payload.order_no,
        "room_no": payload.room_no,
        "item": payload.item,
        "status": "queued",
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }
    _deliveries[delivery["delivery_id"]] = delivery
    return delivery


@app.get("/robot/v1/deliveries/{delivery_id}")
async def get_delivery(delivery_id: str) -> dict[str, Any]:
    delivery = _deliveries.get(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery_not_found")
    return delivery


@app.post("/robot/v1/deliveries/{delivery_id}/advance")
async def advance_delivery(delivery_id: str, payload: AdvanceDeliveryRequest) -> dict[str, Any]:
    delivery = _deliveries.get(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery_not_found")
    delivery["status"] = payload.status
    delivery["updated_at"] = _now().isoformat()
    return delivery


# ---------------------------------------------------------------------------
# 微信：登录 / 支付下单 / 回调模拟（times 参数用于重复回调幂等测试）
# 订阅消息：服务通知下发（第二通道），仅登记发送记录供联调断言
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    code: str


class PayOrderRequest(BaseModel):
    order_no: str
    amount_fen: int = Field(gt=0)
    openid: str
    description: str = "房费"


class CallbackSimulation(BaseModel):
    order_no: str
    target_url: str
    event: Literal["success", "fail"] = "success"
    transaction_id: str | None = None
    times: int = Field(default=1, ge=1, le=10)


class SubscribeMessageRequest(BaseModel):
    openid: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)
    page: str | None = None
    data: dict[str, str] = Field(min_length=1)


def _sign(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(SIGNING_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


@app.post("/wechat/v1/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    digest = hashlib.sha256(payload.code.encode()).hexdigest()[:16]
    return {"openid": f"openid_{digest}", "session_key": secrets.token_hex(16)}


@app.post("/wechat/v1/pay/orders")
async def create_pay_order(payload: PayOrderRequest) -> dict[str, Any]:
    prepay_id = f"prepay_{secrets.token_hex(12)}"
    _payments[payload.order_no] = {
        "order_no": payload.order_no,
        "amount_fen": payload.amount_fen,
        "openid": payload.openid,
        "prepay_id": prepay_id,
        "status": "created",
        "notifications": 0,
    }
    return {
        "prepay_id": prepay_id,
        "pay_params": {
            "appId": "wx-simulated",
            "timeStamp": str(int(_now().timestamp())),
            "nonceStr": secrets.token_hex(8),
            "package": f"prepay_id={prepay_id}",
            "signType": "RSA",
            "paySign": "simulated-signature",
        },
    }


@app.get("/wechat/v1/pay/orders/{order_no}")
async def get_pay_order(order_no: str) -> dict[str, Any]:
    order = _payments.get(order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="pay_order_not_found")
    return order


@app.post("/wechat/v1/pay/simulate-callback")
async def simulate_callback(payload: CallbackSimulation) -> dict[str, Any]:
    order = _payments.get(payload.order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="pay_order_not_found")

    transaction_id = payload.transaction_id or f"txn_{secrets.token_hex(8)}"
    body = {
        "event": payload.event,
        "order_no": payload.order_no,
        "transaction_id": transaction_id,
        "amount_fen": order["amount_fen"],
        "openid": order["openid"],
        "notified_at": _now().isoformat(),
    }
    signature = _sign(body)

    deliveries: list[int | str] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(payload.times):
            try:
                resp = await client.post(
                    payload.target_url, json=body, headers={"X-Sim-Signature": signature}
                )
                deliveries.append(resp.status_code)
            except httpx.HTTPError as exc:
                deliveries.append(f"error: {type(exc).__name__}")

    order["status"] = payload.event
    order["transaction_id"] = transaction_id
    order["notifications"] += payload.times
    return {"transaction_id": transaction_id, "signature": signature, "deliveries": deliveries}


@app.post("/wechat/v1/notify/subscribe")
async def send_subscribe_message(payload: SubscribeMessageRequest) -> dict[str, str]:
    message_id = f"msg_{secrets.token_hex(8)}"
    _notifications.append(
        {
            "message_id": message_id,
            "openid": payload.openid,
            "template_id": payload.template_id,
            "page": payload.page,
            "data": dict(payload.data),
            "status": "sent",
            "sent_at": _now().isoformat(),
        }
    )
    return {"message_id": message_id, "status": "sent"}


@app.get("/wechat/v1/notify/records")
async def list_subscribe_messages(openid: str | None = None) -> dict[str, Any]:
    """调试回读：记录含下发载荷明文，仅联调/断言使用，业务代码禁用（契约 §4.4）。"""
    items = [item for item in _notifications if openid is None or item["openid"] == openid]
    return {"records": items}
