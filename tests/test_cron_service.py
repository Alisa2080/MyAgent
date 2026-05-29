from __future__ import annotations

from types import SimpleNamespace


def test_service_run_once_ticks_when_leader(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    calls = []

    def fake_tick():
        calls.append("tick")
        return SimpleNamespace(due=1, ran=1, succeeded=1, failed=0, skipped=0)

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=fake_tick,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 0
    assert calls == ["tick"]
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert status["leader_state"] == "leader"
    assert status["last_tick"]["ran"] == 1
    assert status["exit_reason"] == "once"


def test_service_run_once_skips_tick_when_follower(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.service import CronService
    from cron.service_state import read_service_status
    from cron.state_store import StateStore

    lease = SchedulerLeaderLease(store=StateStore(), lease_seconds=30)
    lease.try_acquire_or_renew(
        owner_id="host:1:other",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )

    calls = []
    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:2:test",
        pid=2,
        hostname="host",
        tick_fn=lambda: calls.append("tick"),
        clock=lambda: "2026-05-29T10:00:10+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 0
    assert calls == []
    status = read_service_status()
    assert status["leader_state"] == "follower"
    assert status["lease_owner"] == "host:1:other"


def test_service_records_tick_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    def bad_tick():
        raise RuntimeError("tick exploded")

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=bad_tick,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 1
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert "tick exploded" in status["last_error"]
    assert status["exit_reason"] == "once"
