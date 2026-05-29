from __future__ import annotations


def test_service_status_round_trips_json(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import read_service_status, service_status_path, write_service_status

    payload = {
        "version": 1,
        "service": "agent-cron",
        "owner_id": "host:1:abc",
        "pid": 1,
        "hostname": "host",
        "process_state": "running",
        "leader_state": "leader",
        "last_heartbeat_at": "2026-05-29T10:00:00+00:00",
        "last_error": None,
        "exit_reason": None,
    }

    path = service_status_path()
    assert path.name == "status.json"
    assert read_service_status() is None

    write_service_status(payload)

    assert read_service_status() == payload
    assert not path.with_suffix(".json.tmp").exists()


def test_service_status_freshness(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import is_status_fresh

    status = {"last_heartbeat_at": "2026-05-29T10:00:00+00:00"}

    assert is_status_fresh(status, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is True
    assert is_status_fresh(status, now_text="2026-05-29T10:03:00+00:00", stale_after_seconds=120) is False
    assert is_status_fresh({}, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is False
