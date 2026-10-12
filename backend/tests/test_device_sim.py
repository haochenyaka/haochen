import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app.device_sim.main import app

client = TestClient(app)


def setup_function() -> None:
    client.post("/admin/v1/reset")


class _Receiver(BaseHTTPRequestHandler):
    received: list[dict[str, object]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.received.append({"body": body, "signature": self.headers.get("X-Sim-Signature")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_issue_rotate_revokes_old_code() -> None:
    issued = client.post("/lock/v1/credentials", json={"room_no": "8301"}).json()
    assert issued["status"] == "active"
    assert issued["version"] == 1

    rotated = client.post(
        "/lock/v1/credentials/rotate", json={"room_no": "8301", "reason": "daily"}
    ).json()
    assert rotated["version"] == 2
    assert rotated["code"] != issued["code"]

    stale = client.post("/lock/v1/unlock", json={"room_no": "8301", "code": issued["code"]}).json()
    assert stale["accepted"] is False

    fresh = client.post("/lock/v1/unlock", json={"room_no": "8301", "code": rotated["code"]}).json()
    assert fresh["accepted"] is True


def test_revoke_removes_active_credential() -> None:
    client.post("/lock/v1/credentials", json={"room_no": "8302"})
    resp = client.post("/lock/v1/credentials/revoke", json={"room_no": "8302"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"
    assert client.get("/lock/v1/credentials/8302").status_code == 404


def test_version_monotonic_across_revoke() -> None:
    """版本单调：吊销后重新签发继续递增，不回退到 1（否则与本地唯一约束冲突）。"""
    first = client.post("/lock/v1/credentials", json={"room_no": "8304"}).json()
    assert first["version"] == 1
    client.post("/lock/v1/credentials/revoke", json={"room_no": "8304"})
    again = client.post("/lock/v1/credentials", json={"room_no": "8304"}).json()
    assert again["version"] == 2
    rotated = client.post("/lock/v1/credentials/rotate", json={"room_no": "8304"}).json()
    assert rotated["version"] == 3


def test_schedule_next_activate_promotes_and_revokes_current() -> None:
    active = client.post("/lock/v1/credentials", json={"room_no": "8305"}).json()
    scheduled = client.post(
        "/lock/v1/credentials/next",
        json={
            "room_no": "8305",
            "valid_from": "2030-01-01T00:00:00+08:00",
            "valid_to": "2030-01-02T00:00:00+08:00",
        },
    ).json()
    assert scheduled["status"] == "next"
    assert scheduled["version"] == active["version"] + 1
    assert client.get("/lock/v1/credentials/8305/next").json()["code"] == scheduled["code"]

    # 未到窗口：旧码仍可用于开锁（next 不影响当前凭证）
    assert (
        client.post("/lock/v1/unlock", json={"room_no": "8305", "code": active["code"]}).json()[
            "accepted"
        ]
        is True
    )

    promoted = client.post(
        "/lock/v1/credentials/next/activate",
        json={"room_no": "8305", "expected_version": scheduled["version"]},
    ).json()
    assert promoted["status"] == "active"
    assert "code" not in promoted  # 激活不产生新明文
    assert promoted["valid_from"] == scheduled["valid_from"]
    assert client.get("/lock/v1/credentials/8305/next").status_code == 404
    assert client.get("/lock/v1/credentials/8305").json()["code"] == scheduled["code"]
    # 旧当前凭证立即失效、新凭证生效（窗口外仍拒绝，这里用未来窗口仅验证替换语义）
    stale = client.post("/lock/v1/unlock", json={"room_no": "8305", "code": active["code"]}).json()
    assert stale["accepted"] is False


def test_schedule_next_replaces_existing_next() -> None:
    client.post("/lock/v1/credentials", json={"room_no": "8306"})
    first = client.post(
        "/lock/v1/credentials/next",
        json={
            "room_no": "8306",
            "valid_from": "2030-02-01T00:00:00+08:00",
            "valid_to": "2030-02-02T00:00:00+08:00",
        },
    ).json()
    second = client.post(
        "/lock/v1/credentials/next",
        json={
            "room_no": "8306",
            "valid_from": "2030-03-01T00:00:00+08:00",
            "valid_to": "2030-03-02T00:00:00+08:00",
        },
    ).json()
    assert second["version"] == first["version"] + 1
    assert client.get("/lock/v1/credentials/8306/next").json()["code"] == second["code"]


def test_next_activate_and_revoke_guards() -> None:
    assert (
        client.post(
            "/lock/v1/credentials/next/activate", json={"room_no": "8307", "expected_version": 1}
        ).status_code
        == 404
    )
    assert (
        client.post("/lock/v1/credentials/next/revoke", json={"room_no": "8307"}).status_code == 404
    )

    client.post("/lock/v1/credentials", json={"room_no": "8307"})
    scheduled = client.post(
        "/lock/v1/credentials/next",
        json={
            "room_no": "8307",
            "valid_from": "2030-04-01T00:00:00+08:00",
            "valid_to": "2030-04-02T00:00:00+08:00",
        },
    ).json()
    wrong = scheduled["version"] + 1
    assert (
        client.post(
            "/lock/v1/credentials/next/activate",
            json={"room_no": "8307", "expected_version": wrong},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/lock/v1/credentials/next/revoke",
            json={"room_no": "8307", "expected_version": wrong},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/lock/v1/credentials/next/revoke",
            json={"room_no": "8307", "expected_version": scheduled["version"]},
        ).json()["status"]
        == "revoked"
    )
    assert client.get("/lock/v1/credentials/8307/next").status_code == 404
    # 非法窗口（终点不晚于起点）被拒绝
    assert (
        client.post(
            "/lock/v1/credentials/next",
            json={
                "room_no": "8307",
                "valid_from": "2030-04-02T00:00:00+08:00",
                "valid_to": "2030-04-01T00:00:00+08:00",
            },
        ).status_code
        == 422
    )


def test_extend_keeps_code_and_moves_window() -> None:
    issued = client.post("/lock/v1/credentials", json={"room_no": "8308"}).json()
    extended = client.post(
        "/lock/v1/credentials/extend",
        json={
            "room_no": "8308",
            "valid_to": "2030-01-01T00:00:00+08:00",
            "expected_version": issued["version"],
        },
    ).json()
    assert "code" not in extended
    assert extended["version"] == issued["version"]  # 展期不换版本、不换码
    assert client.get("/lock/v1/credentials/8308").json()["code"] == issued["code"]
    assert extended["valid_to"] == "2030-01-01T00:00:00+08:00"

    assert (
        client.post(
            "/lock/v1/credentials/extend",
            json={
                "room_no": "8308",
                "valid_to": "2030-01-01T00:00:00+08:00",
                "expected_version": 99,
            },
        ).status_code
        == 409
    )
    # 终点早于起点被拒绝
    assert (
        client.post(
            "/lock/v1/credentials/extend",
            json={"room_no": "8308", "valid_to": "2000-01-01T00:00:00+08:00"},
        ).status_code
        == 422
    )


def test_unlock_rejects_code_outside_validity_window() -> None:
    issued = client.post(
        "/lock/v1/credentials",
        json={
            "room_no": "8309",
            "valid_from": "2000-01-01T00:00:00+08:00",
            "valid_to": "2000-01-02T00:00:00+08:00",
        },
    ).json()
    expired = client.post(
        "/lock/v1/unlock", json={"room_no": "8309", "code": issued["code"]}
    ).json()
    assert expired["accepted"] is False


def test_fault_injection_error_then_recovery() -> None:
    client.post(
        "/faults/v1/config",
        json={"system": "robot", "mode": "error", "status_code": 503, "remaining": 1},
    )
    payload = {"order_no": "O-1", "room_no": "8301", "item": "矿泉水"}
    assert client.post("/robot/v1/deliveries", json=payload).status_code == 503
    assert client.post("/robot/v1/deliveries", json=payload).status_code == 200


def test_fault_injection_timeout_applies_delay() -> None:
    client.post(
        "/faults/v1/config",
        json={"system": "lock", "mode": "timeout", "sleep_seconds": 0.05, "remaining": 1},
    )
    started = time.monotonic()
    resp = client.get("/lock/v1/credentials/8303")
    elapsed = time.monotonic() - started
    assert resp.status_code == 404
    assert elapsed >= 0.05


def test_pay_callback_simulation_delivers_twice_with_stable_signature() -> None:
    _Receiver.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client.post(
            "/wechat/v1/pay/orders",
            json={"order_no": "O-100", "amount_fen": 19900, "openid": "openid_x"},
        )
        target = f"http://127.0.0.1:{server.server_address[1]}/api/webhooks/wechat/pay"
        result = client.post(
            "/wechat/v1/pay/simulate-callback",
            json={
                "order_no": "O-100",
                "target_url": target,
                "times": 2,
                "transaction_id": "txn_fixed_1",
            },
        ).json()
        assert result["deliveries"] == [200, 200]
    finally:
        server.shutdown()

    assert len(_Receiver.received) == 2
    assert _Receiver.received[0]["body"] == _Receiver.received[1]["body"]
    assert _Receiver.received[0]["signature"] == _Receiver.received[1]["signature"]


def test_subscribe_message_records_payload_and_filters_by_openid() -> None:
    sent = client.post(
        "/wechat/v1/notify/subscribe",
        json={
            "openid": "openid_notify_1",
            "template_id": "tmpl-checkin-v1",
            "page": "pages/orders/index",
            "data": {"room_no": "8301", "floor": "8", "code": "123456", "guide": "退房 12:00 前"},
        },
    ).json()
    assert sent["status"] == "sent"
    assert sent["message_id"].startswith("msg_")

    client.post(
        "/wechat/v1/notify/subscribe",
        json={
            "openid": "openid_notify_2",
            "template_id": "tmpl-checkin-v1",
            "data": {"room_no": "8302", "floor": "8", "code": "654321", "guide": "-"},
        },
    )
    records = client.get("/wechat/v1/notify/records").json()["records"]
    assert [item["openid"] for item in records] == ["openid_notify_1", "openid_notify_2"]
    assert records[0]["message_id"] == sent["message_id"]
    assert records[0]["data"] == {
        "room_no": "8301",
        "floor": "8",
        "code": "123456",
        "guide": "退房 12:00 前",
    }
    assert records[0]["page"] == "pages/orders/index"

    only_second = client.get(
        "/wechat/v1/notify/records", params={"openid": "openid_notify_2"}
    ).json()["records"]
    assert [item["openid"] for item in only_second] == ["openid_notify_2"]

    # 缺字段（无 data）被拒绝，不写入记录
    assert (
        client.post(
            "/wechat/v1/notify/subscribe",
            json={"openid": "openid_notify_1", "template_id": "tmpl-checkin-v1", "data": {}},
        ).status_code
        == 422
    )
