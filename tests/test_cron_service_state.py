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
    assert not list(path.parent.glob(".*.tmp"))


def test_service_status_write_uses_atomic_json_helper(monkeypatch, tmp_path):
    from cron import service_state

    target = tmp_path / "nested" / "status.json"
    payload = {"service": "agent-cron"}
    calls = []

    def fake_atomic_write_json(path, status):
        calls.append((path, status))
        path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(service_state, "atomic_write_json", fake_atomic_write_json)

    service_state.write_service_status(payload, path=target)

    assert target.parent.exists()
    assert calls == [(target, payload)]


def test_service_status_invalid_json_returns_none(tmp_path):
    from cron.service_state import read_service_status

    path = tmp_path / "status.json"
    path.write_text("{invalid", encoding="utf-8")

    assert read_service_status(path=path) is None


def test_service_status_non_dict_json_returns_none(tmp_path):
    from cron.service_state import read_service_status

    path = tmp_path / "status.json"
    path.write_text("[]", encoding="utf-8")

    assert read_service_status(path=path) is None


def test_service_status_freshness(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import is_status_fresh

    status = {"last_heartbeat_at": "2026-05-29T10:00:00+00:00"}

    assert is_status_fresh(status, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is True
    assert is_status_fresh(status, now_text="2026-05-29T10:03:00+00:00", stale_after_seconds=120) is False
    assert is_status_fresh({}, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is False


def test_service_status_freshness_rejects_malformed_datetimes(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import is_status_fresh

    assert (
        is_status_fresh(
            {"last_heartbeat_at": "not-a-date"},
            now_text="2026-05-29T10:01:00+00:00",
            stale_after_seconds=120,
        )
        is False
    )
    assert (
        is_status_fresh(
            {"last_heartbeat_at": "2026-05-29T10:00:00+00:00"},
            now_text="not-a-date",
            stale_after_seconds=120,
        )
        is False
    )


def test_service_status_freshness_clamps_stale_after_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import is_status_fresh

    status = {"last_heartbeat_at": "2026-05-29T10:00:00+00:00"}

    assert is_status_fresh(status, now_text="2026-05-29T10:00:01+00:00", stale_after_seconds=0) is True
    assert is_status_fresh(status, now_text="2026-05-29T10:00:02+00:00", stale_after_seconds=0) is False
