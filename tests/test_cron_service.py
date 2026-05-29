from __future__ import annotations

import sys
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


def test_service_interval_seconds_accepts_float_and_clamps_minimum(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService

    float_interval = CronService(
        interval_seconds=2.5,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: None,
    )
    clamped_interval = CronService(
        interval_seconds=0.25,
        lease_seconds=30,
        owner_id="host:2:test",
        pid=2,
        hostname="host",
        tick_fn=lambda: None,
    )

    assert float_interval.interval_seconds == 2.5
    assert clamped_interval.interval_seconds == 1.0


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


def test_service_records_service_level_loop_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: None,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    def bad_acquire(**_kwargs):
        raise RuntimeError("lease unavailable")

    monkeypatch.setattr(service.lease, "try_acquire_or_renew", bad_acquire)

    assert service.run(once=True) == 1
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert "lease unavailable" in status["last_error"]
    assert status["exit_reason"] == "error"


def test_release_failure_preserves_tick_error(monkeypatch, tmp_path):
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

    def bad_release(_owner_id):
        raise RuntimeError("release exploded")

    monkeypatch.setattr(service.lease, "release", bad_release)

    assert service.run(once=True) == 1
    status = read_service_status()
    assert "tick exploded" in status["last_error"]
    assert "release exploded" not in status["last_error"]


def test_release_failure_after_successful_tick_returns_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: SimpleNamespace(due=1, ran=1, succeeded=1, failed=0, skipped=0),
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    def bad_release(_owner_id):
        raise RuntimeError("release exploded")

    monkeypatch.setattr(service.lease, "release", bad_release)

    assert service.run(once=True) == 1
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert "release exploded" in status["last_error"]


def test_follower_run_once_does_not_release_another_owners_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.service import CronService
    from cron.state_store import StateStore

    lease = SchedulerLeaderLease(store=StateStore(), lease_seconds=30)
    lease.try_acquire_or_renew(
        owner_id="host:1:other",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:2:test",
        pid=2,
        hostname="host",
        tick_fn=lambda: None,
        clock=lambda: "2026-05-29T10:00:10+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 0
    assert lease.current().owner_id == "host:1:other"


def test_signal_handler_does_not_write_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron import service as service_module
    from cron.service import CronService
    from cron.service_state import read_service_status

    handlers = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: None,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )
    writes = []

    monkeypatch.setattr(service_module.signal, "signal", fake_signal)
    monkeypatch.setattr(service, "_write_status", lambda: writes.append("write"), raising=False)

    service.install_signal_handlers()
    handlers[service_module.signal.SIGTERM](service_module.signal.SIGTERM, None)

    assert service.status["process_state"] == "stopping"
    assert service.status["exit_reason"] == f"signal:{service_module.signal.SIGTERM}"
    assert writes == []
    assert read_service_status() is None


def test_initial_status_write_failure_records_error_on_final_write(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status, write_service_status

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: None,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )
    calls = []

    def flaky_write():
        calls.append("write")
        if len(calls) == 1:
            raise RuntimeError("status write exploded")
        write_service_status(service.status)

    monkeypatch.setattr(service, "_write_status", flaky_write)

    assert service.run(once=True) == 1
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert status["exit_reason"] == "error"
    assert "status write exploded" in status["last_error"]


def test_final_status_write_failure_returns_error_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=lambda: SimpleNamespace(due=0, ran=0, succeeded=0, failed=0, skipped=0),
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )
    calls = []

    def flaky_write():
        calls.append("write")
        if len(calls) == 4:
            raise RuntimeError("final status write exploded")

    monkeypatch.setattr(service, "_write_status", flaky_write)

    assert service.run(once=True) == 1
    assert service.status["process_state"] == "exited"
    assert service.status["exit_reason"] == "once"
    assert "final status write exploded" in service.status["last_error"]


def test_default_tick_imports_scheduler_lazily(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService

    monkeypatch.delitem(sys.modules, "cron.scheduler", raising=False)

    CronService(tick_fn=lambda: None)

    assert "cron.scheduler" not in sys.modules


def test_serve_constructs_installs_and_runs(monkeypatch):
    from cron import service as service_module

    calls = []

    class FakeService:
        def __init__(self, *, interval_seconds, lease_seconds):
            calls.append(("init", interval_seconds, lease_seconds))

        def install_signal_handlers(self):
            calls.append(("install",))

        def run(self, *, once):
            calls.append(("run", once))
            return 23

    monkeypatch.setattr(service_module, "CronService", FakeService)

    assert service_module.serve(interval_seconds=2.5, lease_seconds=90, once=True) == 23
    assert calls == [("init", 2.5, 90), ("install",), ("run", True)]
