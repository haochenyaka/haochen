"""审计记录器归属派生：同一 Run 内按子 Agent 派生，写入 slug 彼此独立。"""

from __future__ import annotations

from uuid import uuid4

from app.services.audit_service import AuditRecorder


def test_with_agent_keeps_run_and_switches_slug() -> None:
    run_id = uuid4()
    base = AuditRecorder(run_id=run_id, agent_slug="front-desk")

    derived = base.with_agent("room-service")

    assert derived is not base
    assert derived._run_id == run_id
    assert derived._agent_slug == "room-service"
    assert base._agent_slug == "front-desk", "派生不得改动原记录器"
