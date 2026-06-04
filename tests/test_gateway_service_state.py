from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_gateway_service_state_running_and_transport(tmp_path):
    from gateway.service_state import (
        gateway_status_transport,
        is_gateway_status_running,
        write_gateway_status,
    )

    write_gateway_status(
        tmp_path,
        {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]},
    )

    from gateway.service_state import read_gateway_status

    status = read_gateway_status(tmp_path)
    assert is_gateway_status_running(status) is True
    assert gateway_status_transport(status) == "feishu-ws"


def test_gateway_service_state_marks_stale_not_running(tmp_path):
    import json

    from gateway.service_state import (
        gateway_status_path,
        is_gateway_status_running,
        write_gateway_status,
    )

    write_gateway_status(tmp_path, {"process_state": "running", "transport": "http"})
    path = gateway_status_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    from gateway.service_state import read_gateway_status

    assert is_gateway_status_running(read_gateway_status(tmp_path)) is False
