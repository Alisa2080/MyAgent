from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import os
from types import SimpleNamespace


class _RunCliResult:
    def __init__(self, text: str, exit_code: int) -> None:
        self.text = text
        self.exit_code = exit_code


def _run_cli(argv: list[str]) -> _RunCliResult:
    from agent_cli.main import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return _RunCliResult(stdout.getvalue() + stderr.getvalue(), exit_code)


def test_create_defaults_to_origin_when_session_id_present(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {
                "job_id": "job-1",
                "name": "report",
                "schedule": "30m",
                "next_run_at": "soon",
                "skills": [],
            },
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        session_id="session-1",
    )

    assert result.exit_code == 0
    assert "Created cron job job-1" in result.text
    assert calls[0][0] == "create"
    assert calls[0][1] == "session-1"
    assert calls[0][2]["deliver"] == "origin"


def test_top_level_create_defaults_to_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {"job_id": "job-1", "name": "report", "schedule": "30m"},
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        top_level=True,
    )

    assert result.exit_code == 0
    assert calls[0][1] is None
    assert calls[0][2]["deliver"] == "local"


def test_create_accepts_webhook_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {"job_id": "job-1", "name": "report", "schedule": "30m"},
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        deliver="webhook:https://example.invalid/hook",
        top_level=True,
    )

    assert result.exit_code == 0
    assert calls[0][2]["deliver"] == "webhook:https://example.invalid/hook"


def test_create_multi_target_origin_binds_session_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {"job_id": "job-1", "name": "report", "schedule": "30m"},
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        deliver="origin,local",
        session_id="session-1",
    )

    assert result.exit_code == 0
    assert calls[0][1] == "session-1"
    assert calls[0][2]["deliver"] == "origin,local"


def test_top_level_origin_delivery_is_rejected():
    import agent_cli.cron_commands as cron_commands

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        deliver="origin",
        top_level=True,
    )

    assert result.exit_code == 2
    assert "origin delivery requires an active CLI session" in result.text


def test_update_origin_delivery_binds_session_thread(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {"success": True, "job": {"job_id": "job-1"}}

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.update_cron_job(
        job_id="job-1",
        deliver="origin",
        session_id="session-1",
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "update",
            "session-1",
            {
                "job_id": "job-1",
                "deliver": "origin",
                "origin": {
                    "source_type": "cli",
                    "session_id": "session-1",
                    "thread_id": "session-1",
                },
            },
        )
    ]


def test_update_multi_target_origin_binds_session_thread(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {"success": True, "job": {"job_id": "job-1"}}

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.update_cron_job(
        job_id="job-1",
        deliver="origin,local",
        session_id="session-1",
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "update",
            "session-1",
            {
                "job_id": "job-1",
                "deliver": "origin,local",
                "origin": {
                    "source_type": "cli",
                    "session_id": "session-1",
                    "thread_id": "session-1",
                },
            },
        )
    ]


def test_top_level_update_origin_delivery_is_rejected():
    import agent_cli.cron_commands as cron_commands

    result = cron_commands.update_cron_job(
        job_id="job-1",
        deliver="origin",
        top_level=True,
    )

    assert result.exit_code == 2
    assert "origin delivery requires an active CLI session" in result.text


def test_status_renders_scheduler_and_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [{"id": "a"}])
    monkeypatch.setattr(cron_commands, "_delivery_stats_lines", lambda: ["Delivery queue: pending=0 failed=0 dead=0 delivered=0"])

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Scheduler service:" in result.text
    assert "Service status:" in result.text
    assert "Cron sqlite:" in result.text
    assert "Delivery adapters:" in result.text
    assert "Jobs: 1" in result.text


def test_cron_status_includes_automatic_scheduling_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "_service_status_lines", lambda: [])
    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])
    monkeypatch.setattr(cron_commands, "_delivery_stats_lines", lambda: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_status()

    assert "Service manager: systemd-user installed active enabled" in result.text
    assert "Automatic scheduling: enabled" in result.text


def test_cron_service_status_renders_delivery_summary(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick={
                "due": 0,
                "ran": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "delivery": {
                    "recovered_stale": 1,
                    "claimed": 2,
                    "delivered": 1,
                    "failed": 1,
                    "dead": 0,
                    "error": None,
                },
            },
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_service_status()

    assert "Last tick: due=0 ran=0 succeeded=0 failed=0 skipped=0" in result.text
    assert "Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0" in result.text


def test_serve_cron_calls_service(monkeypatch):
    from agent_cli import cron_commands

    calls = []

    monkeypatch.setattr(
        "cron.service.serve",
        lambda interval_seconds=60, lease_seconds=180, once=False: calls.append(
            (interval_seconds, lease_seconds, once)
        ) or 0,
    )

    result = cron_commands.serve_cron(interval_seconds=5, lease_seconds=20, once=True)

    assert result.exit_code == 0
    assert result.text == "Cron service exited."
    assert calls == [(5, 20, True)]


def test_serve_cron_propagates_service_exit_code(monkeypatch):
    from agent_cli import cron_commands

    monkeypatch.setattr(
        "cron.service.serve",
        lambda interval_seconds=60, lease_seconds=180, once=False: 7,
    )

    result = cron_commands.serve_cron(interval_seconds=5, lease_seconds=20, once=True)

    assert result.exit_code == 7
    assert result.text == "Cron service exited."


def test_cron_status_includes_service_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_state import write_service_status

    write_service_status(
        {
            "version": 1,
            "service": "agent-cron",
            "owner_id": "host:1:abc",
            "pid": 1,
            "hostname": "host",
            "process_state": "running",
            "leader_state": "leader",
            "lease_owner": "host:1:abc",
            "lease_expires_at": "2026-05-29T10:03:00+00:00",
            "started_at": "2026-05-29T10:00:00+00:00",
            "last_heartbeat_at": "2026-05-29T10:01:00+00:00",
            "last_tick_started_at": "2026-05-29T10:01:00+00:00",
            "last_tick_finished_at": "2026-05-29T10:01:01+00:00",
            "last_tick": {"due": 1, "ran": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            "last_error": None,
            "exit_reason": None,
        }
    )
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])

    result = cron_commands.cron_status()

    assert "Scheduler service: running" in result.text
    assert "Leader state: leader" in result.text
    assert "Last tick: due=1 ran=1 succeeded=1 failed=0 skipped=0" in result.text


def test_cron_status_includes_service_error_exit_and_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import now
    from cron.leader import SchedulerLeaderLease
    from cron.service_state import write_service_status

    now_text = now().isoformat()
    lease_state = SchedulerLeaderLease(lease_seconds=180).try_acquire_or_renew(
        owner_id="host:1:abc",
        pid=1,
        hostname="host",
        now_text=now_text,
    )
    write_service_status(
        {
            "process_state": "exited",
            "leader_state": "leader",
            "pid": 1,
            "last_heartbeat_at": now_text,
            "last_error": "RuntimeError: boom",
            "exit_reason": "error",
        }
    )
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])

    result = cron_commands.cron_status()

    assert "Last service error: RuntimeError: boom" in result.text
    assert "Exit reason: error" in result.text
    assert "Lease owner: host:1:abc" in result.text
    assert f"Lease expires: {lease_state.expires_at}" in result.text


def test_cron_status_handles_missing_and_malformed_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_state import service_status_path

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])

    missing_result = cron_commands.cron_status()
    assert "Scheduler service: unknown" in missing_result.text

    service_status_path().write_text("{invalid", encoding="utf-8")
    malformed_result = cron_commands.cron_status()
    assert "Scheduler service: unknown" in malformed_result.text


def test_cron_doctor_reports_running_service(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import now
    from cron.service_manager import ServiceStatus
    from cron.service_state import write_service_status

    def fail_scheduler_check():
        raise AssertionError("doctor must not depend on the REPL scheduler")

    monkeypatch.setattr(
        cron_commands,
        "is_cron_scheduler_running",
        fail_scheduler_check,
        raising=False,
    )
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at=now().isoformat(),
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )
    write_service_status(
        {
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": now().isoformat(),
        }
    )

    result = cron_commands.cron_doctor()

    assert "[ok] cron service: running (systemd-user)" in result.text
    assert "scheduler is stopped" not in result.text


def test_cron_doctor_warns_when_service_is_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=False,
            enabled=False,
            active=False,
            pid=None,
            detail="not installed",
            error=None,
            heartbeat_fresh=False,
            process_state=None,
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service: not installed; run `agent cron service install`" in result.text


def test_cron_doctor_warns_when_service_heartbeat_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus
    from cron.service_state import write_service_status

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=False,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-05-29T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )
    write_service_status(
        {
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": "2026-05-29T10:00:00+00:00",
        }
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service heartbeat: stale; run `agent cron service restart`" in result.text


def test_cron_doctor_suggests_service_install_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=False,
            enabled=False,
            active=False,
            pid=None,
            detail="not installed",
            error=None,
            heartbeat_fresh=False,
            process_state=None,
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service: not installed; run `agent cron service install`" in result.text
    assert "cron service heartbeat: missing" not in result.text


def test_cron_doctor_suggests_service_restart_when_heartbeat_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=False,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service heartbeat: stale; run `agent cron service restart`" in result.text
    assert "automatic scheduling may be stopped" not in result.text


def test_cron_doctor_warns_when_service_pid_is_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: False)
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=99999,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service pid is not running; run `agent cron service restart`" in result.text


def test_cron_doctor_warns_when_status_file_pid_is_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import now
    from cron.service_manager import ServiceRuntimeStatus
    from cron.service_state import read_service_status, write_service_status

    payload = {
        "version": 1,
        "service": "agent-cron",
        "owner_id": "host:99999999:abc",
        "pid": 99999999,
        "hostname": "host",
        "process_state": "running",
        "leader_state": "leader",
        "last_heartbeat_at": now().isoformat(),
        "last_tick": {"due": 0, "ran": 0, "succeeded": 0, "failed": 0, "skipped": 0},
        "last_error": None,
        "exit_reason": None,
    }
    write_service_status(payload)

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: False)
    monkeypatch.setattr(
        "cron.service_manager.service_runtime_status",
        lambda: ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=None,
            detail="active",
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service status pid is not running; run `agent cron service restart`" in result.text
    assert read_service_status() == payload


def test_cron_doctor_suggests_force_install_when_service_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=False,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service: installed but disabled; run `agent cron service install --force`" in result.text


def test_delivery_stats_lines_labels_origin_poll_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.delivery import JobRunResult, enqueue_result

    enqueue_result(
        {
            "id": "job-origin",
            "name": "Origin",
            "deliver": "origin",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    lines = cron_commands._delivery_stats_lines()

    assert "Origin poll pending: 1" in lines


def test_status_includes_subprocess_timeout_when_subprocess_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
    monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "45")
    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])
    monkeypatch.setattr(cron_commands, "_delivery_stats_lines", lambda: [])

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Runner mode: ok (subprocess)" in result.text
    assert "Subprocess timeout: 45s" in result.text


def test_cron_doctor_warns_for_prod_inprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 1
    assert "[ok] effective profile: prod (runtime)" in result.text
    assert "[ok] runner mode: inprocess" in result.text
    assert (
        "[warn] prod/hosted cron service should use AGENT_CRON_RUNNER_MODE=subprocess"
        in result.text
    )


def test_cron_doctor_runs_worker_smoke_for_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary, WorkerSmokeResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.worker_protocol_smoke",
        lambda: WorkerSmokeResult(True),
    )
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 0
    assert "[ok] runner_worker smoke: ok" in result.text


def test_cron_doctor_warns_when_subprocess_timeout_is_too_small(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
    monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "5")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary, WorkerSmokeResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.worker_protocol_smoke",
        lambda: WorkerSmokeResult(True),
    )
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 1
    assert "[ok] subprocess timeout: 5s" in result.text
    assert (
        "[warn] subprocess timeout is very small; use at least 30s for production cron jobs"
        in result.text
    )


def test_cron_doctor_fails_when_worker_smoke_cannot_use_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary, WorkerSmokeResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.worker_protocol_smoke",
        lambda: WorkerSmokeResult(False, "runner tmp unavailable", severity="fail"),
    )
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 2
    assert "[fail] runner_worker smoke: runner tmp unavailable" in result.text


def test_cron_doctor_warns_when_worker_smoke_times_out(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary, WorkerSmokeResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.worker_protocol_smoke",
        lambda: WorkerSmokeResult(
            False,
            "runner_worker smoke timed out after 10s",
            severity="warn",
        ),
    )
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 1
    assert "[warn] runner_worker smoke: runner_worker smoke timed out after 10s" in result.text


def test_cron_doctor_fails_when_runner_tmp_inspection_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(
            total=0,
            stale=0,
            oldest_age_seconds=None,
            path=tmp_path,
            error="permission denied",
        ),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 2
    assert "[fail] runner tmp residuals: permission denied" in result.text


def test_cron_doctor_cleanup_runner_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpCleanupResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.cleanup_runner_tmp",
        lambda: RunnerTmpCleanupResult(removed=2, failed=0, remaining=1, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None, cleanup_runner_tmp=True)

    assert "runner tmp cleanup: removed=2 remaining=1" in result.text


def test_cron_doctor_cleanup_runner_tmp_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpCleanupResult, RunnerTmpSummary

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.cleanup_runner_tmp",
        lambda: RunnerTmpCleanupResult(
            removed=0,
            failed=1,
            remaining=0,
            path=tmp_path,
            error="not a directory",
        ),
    )
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None, cleanup_runner_tmp=True)

    assert result.exit_code == 2
    assert "[fail] runner tmp cleanup: not a directory" in result.text


def test_cron_status_renders_effective_profile_and_tmp_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary

    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])
    monkeypatch.setattr(cron_commands, "_delivery_stats_lines", lambda: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=3, stale=1, oldest_age_seconds=90000, path=tmp_path),
    )

    result = cron_commands.cron_status(cli_profile=None)

    assert "Effective profile: prod (runtime)" in result.text
    assert "Runner tmp: total=3 stale=1 oldest=90000s" in result.text


def test_tick_returns_failure_exit_when_job_fails(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    tick_result = SimpleNamespace(
        due=1,
        ran=1,
        succeeded=0,
        failed=1,
        skipped=0,
        results=[SimpleNamespace(job_id="job-1", success=False, error="boom")],
    )
    monkeypatch.setattr(cron_commands, "cron_tick", lambda: tick_result)

    result = cron_commands.run_tick()

    assert result.exit_code == 1
    assert "failed=1" in result.text
    assert "job-1" in result.text


def test_tick_renders_delivery_summary(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    tick_result = SimpleNamespace(
        due=0,
        ran=0,
        succeeded=0,
        failed=0,
        skipped=0,
        delivery=SimpleNamespace(
            recovered_stale=1,
            claimed=2,
            delivered=1,
            failed=1,
            dead=0,
            error=None,
        ),
        results=[],
    )
    monkeypatch.setattr(cron_commands, "cron_tick", lambda: tick_result)

    result = cron_commands.run_tick()

    assert result.exit_code == 0
    assert "Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0" in result.text


def test_tick_reports_current_scheduler_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.leader import SchedulerLeaderLease

    tick_result = SimpleNamespace(
        due=0,
        ran=0,
        succeeded=0,
        failed=0,
        skipped=0,
        results=[],
    )
    lease_state = SchedulerLeaderLease(lease_seconds=180).try_acquire_or_renew(
        owner_id="host:1:abc",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )
    monkeypatch.setattr(cron_commands, "cron_tick", lambda: tick_result)

    result = cron_commands.run_tick()

    assert result.exit_code == 0
    assert (
        f"Scheduler lease owner: host:1:abc expires={lease_state.expires_at}"
    ) in result.text


def test_remove_reports_missing_job(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(
        cron_commands,
        "run_cronjob_action",
        lambda action, **kwargs: {"success": True, "removed": False},
    )

    result = cron_commands.simple_job_action("remove", job_id="missing")

    assert result.exit_code == 2
    assert "Cron job not found: missing" in result.text


def test_run_action_uses_ran_label(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(
        cron_commands,
        "run_cronjob_action",
        lambda action, **kwargs: {
            "success": True,
            "job": {"job_id": "job-1"},
        },
    )

    result = cron_commands.simple_job_action("run", job_id="job-1")

    assert result.exit_code == 0
    assert result.text == "Ran cron job job-1."


def test_cron_status_includes_delivery_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Delivery queue:" in result.text
    assert "pending=1" in result.text


def test_cron_status_includes_sqlite_state_counts_and_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import create_job

    create_job(prompt="write report", schedule="30m", deliver="local")

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Cron sqlite:" in result.text
    assert "Job states: scheduled=1" in result.text
    assert "Delivery adapters:" in result.text
    assert "feishu" in result.text
    assert "local" in result.text
    assert "origin" in result.text
    assert "webhook" in result.text
    assert "wecom" in result.text


def test_cron_doctor_reports_delivery_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor()

    assert "delivery db" in result.text.lower()
    assert result.exit_code in {0, 1}


def test_cron_doctor_accepts_multi_target_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import now
    from cron.service_manager import ServiceStatus
    from cron.service_state import write_service_status

    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at=now().isoformat(),
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )
    write_service_status(
        {
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": now().isoformat(),
        }
    )

    monkeypatch.setattr(
        cron_commands,
        "list_jobs",
        lambda include_disabled=False: [
            {
                "id": "job-1",
                "deliver": "origin,local",
                "origin": {
                    "source_type": "cli",
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                },
                "next_run_at": "2026-05-28T10:00:00+00:00",
            }
        ],
    )

    result = cron_commands.cron_doctor()

    assert "unsupported delivery target" not in result.text
    assert "delivery invalid" not in result.text
    assert "service env permissions are too broad" not in result.text


def test_cron_doctor_checks_subprocess_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
    monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "45")

    from agent_cli import cron_commands

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor()

    assert "runner mode: subprocess" in result.text
    assert "subprocess timeout: 45s" in result.text
    assert "runner_worker smoke: ok" in result.text
    assert "name 'sys' is not defined" not in result.text


def test_cron_doctor_reports_worker_help_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")

    from agent_cli import cron_commands
    import subprocess

    class FailedHelp:
        returncode = 2
        stderr = b"worker help failed"
        stdout = b""

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: FailedHelp())

    result = cron_commands.cron_doctor()

    assert result.exit_code == 2
    assert "runner_worker smoke: runner_worker smoke exited with code 2" in result.text
    assert "worker help failed" in result.text


def test_cron_doctor_reports_invalid_jobs_json_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.paths import get_jobs_file

    get_jobs_file().parent.mkdir(parents=True, exist_ok=True)
    get_jobs_file().write_text("{invalid", encoding="utf-8")

    result = cron_commands.cron_doctor()

    assert result.exit_code == 2
    assert "jobs file JSON is invalid" in result.text


def test_test_delivery_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    result = cron_commands.test_delivery(target="local")

    assert result.exit_code == 0
    assert "test-delivery" in result.text
    assert "delivered" in result.text or "pending" in result.text


def test_test_delivery_multi_target(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    result = cron_commands.test_delivery(target="origin,local", session_id="session-1")

    assert result.exit_code == 0
    assert "target=origin" in result.text
    assert "target=local" in result.text


def test_test_delivery_dead_target_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    result = cron_commands.test_delivery(target="webhook")

    assert result.exit_code == 2
    assert "status=dead" in result.text


def _register_env_recording_feishu(monkeypatch, sent_envs):
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(True)

        def token_smoke(self):
            return SendResult(True)

        def send_text(self, target, message):
            sent_envs.append(
                {
                    "FEISHU_APP_ID": os.environ.get("FEISHU_APP_ID"),
                    "FEISHU_APP_SECRET": os.environ.get("FEISHU_APP_SECRET"),
                    "target_id": target.target_id,
                    "text": message.text,
                }
            )
            missing = [
                key
                for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
                if not os.environ.get(key)
            ]
            if missing:
                return SendResult(
                    False,
                    error=f"missing required Feishu environment variables: {', '.join(missing)}",
                )
            return SendResult(True)

    monkeypatch.setattr(gateway_registry, "_ADAPTER_FACTORIES", [])
    monkeypatch.setattr(gateway_registry, "_DEFAULT_GATEWAY_REGISTRY", None)
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())


def test_test_delivery_uses_service_env_for_feishu(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    from agent_cli import cron_commands
    from cron.service_env import write_service_env

    write_service_env(
        {
            "FEISHU_APP_ID": "service-app",
            "FEISHU_APP_SECRET": "service-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)

    result = cron_commands.test_delivery(target="feishu:oc_service")

    assert result.exit_code == 0
    assert "status=delivered" in result.text
    assert "service-app" not in result.text
    assert "service-secret" not in result.text
    assert len(sent_envs) == 1
    assert sent_envs[0]["FEISHU_APP_ID"] == "service-app"
    assert sent_envs[0]["FEISHU_APP_SECRET"] == "service-secret"
    assert sent_envs[0]["target_id"] == "oc_service"
    assert sent_envs[0]["text"].startswith(
        "Cron job update: test-delivery\nstatus: ok\njob_id: test-delivery"
    )
    assert os.environ.get("FEISHU_APP_ID") is None
    assert os.environ.get("FEISHU_APP_SECRET") is None


def test_test_delivery_shell_env_overrides_service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")

    from agent_cli import cron_commands
    from cron.service_env import write_service_env

    write_service_env(
        {
            "FEISHU_APP_ID": "service-app",
            "FEISHU_APP_SECRET": "service-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)

    result = cron_commands.test_delivery(target="feishu:oc_shell")

    assert result.exit_code == 0
    assert "status=delivered" in result.text
    assert sent_envs[0]["FEISHU_APP_ID"] == "shell-app"
    assert sent_envs[0]["FEISHU_APP_SECRET"] == "shell-secret"
    assert os.environ["FEISHU_APP_ID"] == "shell-app"
    assert os.environ["FEISHU_APP_SECRET"] == "shell-secret"


def test_cron_status_lists_registered_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_status
    from cron.delivery_adapters import AdapterValidation, DeliveryResult
    import cron.delivery_registry as delivery_registry

    class FakeSlackAdapter:
        key = "slack"
        active_dispatch = True

        def validate(self, target, job):
            return AdapterValidation(True)

        def deliver(self, event, job, run):
            return DeliveryResult(True)

    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: FakeSlackAdapter())
    try:
        result = cron_status()
    finally:
        delivery_registry.clear_delivery_adapter_factories()

    assert "Delivery adapters:" in result.text
    assert "slack" in result.text


def test_cron_status_shows_next_due_and_latest_failed_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_status
    from cron.jobs import create_job
    from cron.state_store import StateStore

    next_job = create_job(
        prompt="write report",
        schedule="every 30m",
        name="next-due",
        deliver="local",
    )
    failed_job = create_job(
        prompt="write report",
        schedule="every 30m",
        name="failed-job",
        deliver="local",
    )
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=failed_job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    store.complete_run(
        run_id,
        success=False,
        output_path=None,
        final_response=None,
        error="idle",
        next_run_at=next_job["next_run_at"],
        completed=False,
        exit_reason="idle_timeout",
    )

    result = cron_status()

    assert "Next due:" in result.text
    assert "next-due" in result.text
    assert "Latest failed run:" in result.text
    assert "idle_timeout" in result.text
    assert "idle" in result.text


def test_cron_doctor_lists_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.text
    assert "local" in result.text
    assert "origin" in result.text
    assert "webhook" in result.text
    assert "wecom" in result.text


def test_cron_doctor_lists_feishu_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.text
    assert "feishu" in result.text


def test_cron_doctor_fails_active_feishu_job_missing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    import gateway.registry as gateway_registry
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.service_env import write_service_env
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            missing = [
                name
                for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
                if not os.environ.get(name)
            ]
            if missing:
                return SendResult(
                    False,
                    error=f"missing required Feishu environment variables: {', '.join(missing)}",
                )
            return SendResult(True)

        def send_text(self, target, message):
            return SendResult(True)

        def token_smoke(self):
            return SendResult(True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        job = create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 2
    assert f"active job {job['id']} feishu delivery invalid" in result.text
    assert "missing required Feishu environment variables: FEISHU_APP_ID, FEISHU_APP_SECRET" in result.text


def test_cron_doctor_fails_active_feishu_job_missing_chat_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET deliver = ?, delivery_targets_json = NULL WHERE id = ?",
            ("feishu:", job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "feishu delivery requires explicit target: feishu:<chat_id>" in result.text


def test_cron_doctor_passes_explicit_feishu_target_to_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import gateway.registry as gateway_registry
    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.service_env import ServiceEnvFileStatus
    from gateway.contracts import SendResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(
        "cron.service_env.inspect_service_env_file",
        lambda path=None: ServiceEnvFileStatus(
            path=tmp_path / "cron" / "service.env",
            exists=True,
            readable=True,
            permissions_ok=True,
        ),
    )
    monkeypatch.setattr(
        "cron.service_env.read_service_env",
        lambda path=None: {"FEISHU_APP_ID": "app_id", "FEISHU_APP_SECRET": "app_secret"},
    )
    validated_targets = []

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            validated_targets.append(target)
            return SendResult(True)

        def send_text(self, target, message):
            raise AssertionError("doctor must not send a Feishu message")

        def token_smoke(self):
            return SendResult(True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")

        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 0
    assert validated_targets
    assert validated_targets[-1].platform == "feishu"
    assert validated_targets[-1].target_type == "chat_id"
    assert validated_targets[-1].target_id == "oc_123"


def test_cron_doctor_feishu_token_smoke_invalid_credentials_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import gateway.registry as gateway_registry
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.service_env import write_service_env
    from gateway.contracts import SendResult

    validated_targets = []

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            validated_targets.append(target)
            return SendResult(True)

        def send_text(self, target, message):
            raise AssertionError("doctor must not send a Feishu message")

        def token_smoke(self):
            return SendResult(
                False,
                error="feishu token API error 99991663: invalid app credentials",
                retryable=False,
            )

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        write_service_env({"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "secret"})
        job = create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")

        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 2
    assert validated_targets
    assert validated_targets[-1].platform == "feishu"
    assert validated_targets[-1].target_type == "chat_id"
    assert validated_targets[-1].target_id == "oc_123"
    assert (
        f"active job {job['id']} feishu token smoke failed: "
        "feishu token API error 99991663: invalid app credentials"
    ) in result.text


def test_cron_doctor_feishu_token_smoke_temporary_failure_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import gateway.registry as gateway_registry
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.service_env import write_service_env
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(True)

        def send_text(self, target, message):
            raise AssertionError("doctor must not send a Feishu message")

        def token_smoke(self):
            return SendResult(
                False,
                error="feishu token HTTP 503: service unavailable",
                retryable=True,
            )

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        write_service_env({"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "secret"})
        job = create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")

        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 1
    assert (
        f"active job {job['id']} feishu token smoke temporary failure: "
        "feishu token HTTP 503: service unavailable"
    ) in result.text


def test_cron_doctor_uses_stored_wecom_target_when_env_changes(monkeypatch, tmp_path):
    valid_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key"
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", valid_url)

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job

    job = create_job(prompt="write report", schedule="30m", deliver="wecom")
    monkeypatch.delenv("AGENT_CRON_WECOM_WEBHOOK_URL", raising=False)

    result = cron_doctor()

    assert result.exit_code != 2
    assert "wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL" not in result.text
    assert f"active job {job['id']} delivery invalid" not in result.text


def test_cron_doctor_fails_legacy_wecom_job_without_default_url(monkeypatch, tmp_path):
    valid_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key"
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", valid_url)

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="wecom")
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET delivery_targets_json = NULL WHERE id = ?",
            (job["id"],),
        )
    monkeypatch.delenv("AGENT_CRON_WECOM_WEBHOOK_URL", raising=False)

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL" in result.text


def test_cron_doctor_fails_active_wecom_job_with_malformed_url(monkeypatch, tmp_path):
    valid_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key"
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", valid_url)

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="wecom")
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET deliver = ?, delivery_targets_json = NULL WHERE id = ?",
            ("wecom:https://example.invalid/hook", job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "wecom webhook URL host must be qyapi.weixin.qq.com" in result.text


def test_cron_doctor_fails_active_wecom_job_with_invalid_port_url(monkeypatch, tmp_path):
    valid_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key"
    invalid_url = "https://qyapi.weixin.qq.com:bad/cgi-bin/webhook/send?key=abc"
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", valid_url)

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="wecom")
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET deliver = ?, delivery_targets_json = NULL WHERE id = ?",
            (f"wecom:{invalid_url}", job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "wecom webhook URL port is invalid" in result.text


def test_cron_doctor_fails_stored_wecom_target_with_malformed_url(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    stored_targets = [
        {
            "raw": "wecom",
            "target_type": "platform",
            "adapter_key": "wecom",
            "address": "https://example.invalid/hook",
            "thread_id": None,
            "metadata": {},
        }
    ]
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET delivery_targets_json = ? WHERE id = ?",
            (json.dumps(stored_targets), job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} wecom delivery invalid" in result.text
    assert "wecom webhook URL host must be qyapi.weixin.qq.com" in result.text


def test_cron_run_dry_run_does_not_create_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import run_cron_job

    job = create_job(prompt="hello", schedule="every 5m")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})

    result = run_cron_job(job_id=job["id"], dry_run=True, now_text="2026-05-29T10:00:00+00:00")

    assert result.exit_code == 0
    assert "would_claim" in result.text
    assert StateStore().list_runs(job_id=job["id"]) == []


def test_cron_runs_lists_recent_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from datetime import datetime, timezone
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore, utc_now
    from agent_cli.cron_commands import list_cron_runs

    fixed_now = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.state_store.utc_now", lambda: fixed_now)

    job = create_job(prompt="hello", schedule="every 5m")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=1)[0]
    store.mark_run_started(claimed["run"]["id"])
    store.complete_run(
        claimed["run"]["id"],
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at=None,
        completed=False,
    )

    result = list_cron_runs(job_id=job["id"], limit=10)

    assert result.exit_code == 0
    assert claimed["run"]["id"][:8] in result.text
    assert "succeeded" in result.text


def test_cron_logs_shows_latest_error_and_output(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_logs

    job = create_job(prompt="hello", schedule="every 5m", name="Daily")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=1)[0]
    store.complete_run(
        claimed["run"]["id"],
        success=False,
        output_path="/tmp/out.md",
        final_response="",
        error="boom",
        next_run_at=None,
        completed=False,
        run_status="failed",
    )

    result = cron_logs(job_id=job["id"], limit=5)

    assert result.exit_code == 0
    assert "Daily" in result.text
    assert "boom" in result.text
    assert "/tmp/out.md" in result.text


def test_retry_delivery_resets_failed_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore
    from agent_cli.cron_commands import retry_delivery

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        run_id="run-1",
        job_name="Job",
        run_at="2026-05-29T10:00:00+00:00",
        target="webhook:https://example.invalid",
        target_type="webhook",
        adapter_key="webhook",
        payload={"ok": True},
        status="failed",
        final_response="done",
        output_path="/tmp/out.md",
    )
    store.update_event(event["id"], status="failed", last_error="HTTP 500")

    result = retry_delivery(event["id"])
    updated = store.get(event["id"])

    assert result.exit_code == 0
    assert updated["status"] == "pending"
    assert updated["last_error"] is None


def test_cron_status_shows_running_queued_and_next_due(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_status

    first = create_job(
        prompt="first",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_one",
    )
    second = create_job(
        prompt="second",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_one",
    )
    update_job(first["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    update_job(second["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    StateStore().claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    result = cron_status()

    assert result.exit_code == 0
    assert "Running:" in result.text
    assert "Queued:" in result.text
    assert "Next due:" in result.text


def test_cron_run_dry_run_shows_resolved_execution_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from agent_cli.cron_commands import run_cron_job

    job = create_job(
        prompt="hello",
        schedule="every 5m",
        deliver="local",
        idle_timeout_seconds=30,
        max_runtime_seconds=90,
    )
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})

    result = run_cron_job(job_id=job["id"], dry_run=True, now_text="2026-05-29T10:00:00+00:00")

    assert result.exit_code == 0
    assert "Due: yes" in result.text
    assert "Decision: would_claim" in result.text
    assert "Timeouts: idle=30s max_runtime=90s" in result.text
    assert "Delivery targets: local" in result.text
    assert "Next scheduled:" in result.text


def test_cron_run_dry_run_missing_job_returns_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import run_cron_job

    result = run_cron_job(
        job_id="missing",
        dry_run=True,
        now_text="2026-05-29T10:00:00+00:00",
    )

    assert result.exit_code == 2
    assert "Cron job not found: missing." in result.text


def test_cron_create_and_edit_pass_concurrency_flags(monkeypatch):
    import agent_cli.main as main

    calls = []

    def fake_create(**kwargs):
        calls.append(("create", kwargs))
        return main.cron_commands.CronCommandResult("created")

    def fake_update(**kwargs):
        calls.append(("edit", kwargs))
        return main.cron_commands.CronCommandResult("updated")

    monkeypatch.setattr(main.cron_commands, "create_cron_job", fake_create)
    monkeypatch.setattr(main.cron_commands, "update_cron_job", fake_update)

    parser = main.build_parser()
    create_args = parser.parse_args([
        "cron",
        "create",
        "every 5m",
        "hello",
        "--concurrency-key",
        "repo:a",
        "--concurrency-policy",
        "skip_if_running",
    ])
    edit_args = parser.parse_args([
        "cron",
        "edit",
        "job-1",
        "--concurrency-key",
        "repo:b",
        "--concurrency-policy",
        "queue_all",
    ])

    main._run_cron_command(create_args)
    main._run_cron_command(edit_args)

    assert calls[0] == (
        "create",
        {
            "schedule": "every 5m",
            "prompt": "hello",
            "top_level": True,
            "name": None,
            "deliver": None,
            "repeat": None,
            "skills": None,
            "script": None,
            "workdir": None,
            "concurrency_key": "repo:a",
            "concurrency_policy": "skip_if_running",
        },
    )
    assert calls[1] == (
        "edit",
        {
            "job_id": "job-1",
            "top_level": True,
            "concurrency_key": "repo:b",
            "concurrency_policy": "queue_all",
        },
    )


def test_install_cron_service_renders_manager_result(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceCommandResult

    captured = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return ServiceCommandResult("installed ok", exit_code=0)

    monkeypatch.setattr("cron.service_manager.install_service", fake_install)

    result = cron_commands.install_cron_service(
        interval_seconds=30,
        lease_seconds=90,
        force=True,
        cli_profile="prod",
    )

    assert result.exit_code == 0
    assert result.text == "installed ok"
    assert captured == {
        "interval_seconds": 30,
        "lease_seconds": 90,
        "force": True,
        "cli_profile": "prod",
    }


def test_cron_service_status_renders_composed_status(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick={
                "due": 1,
                "ran": 1,
                "succeeded": 1,
                "failed": 0,
                "skipped": 0,
            },
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_service_status()

    assert result.exit_code == 0
    assert "Service manager: systemd-user installed active enabled" in result.text
    assert "PID: 123" in result.text
    assert "Heartbeat: fresh" in result.text
    assert "Last tick: due=1 ran=1 succeeded=1 failed=0 skipped=0" in result.text


def test_cron_service_status_not_ready_when_disabled(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=False,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_service_status()

    assert result.exit_code == 0
    assert "Service manager: systemd-user installed active disabled" in result.text
    assert "Automatic scheduling: not-ready" in result.text


def test_cron_service_env_set_list_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    set_result = _run_cli(["cron", "service", "env", "set", "FEISHU_APP_SECRET", "secret"])
    assert set_result.exit_code == 0
    assert "Set service env FEISHU_APP_SECRET" in set_result.text
    assert "cron service restart" in set_result.text
    assert "launchd" in set_result.text

    list_result = _run_cli(["cron", "service", "env", "list"])
    assert list_result.exit_code == 0
    assert "FEISHU_APP_SECRET=********" in list_result.text
    assert "secret" not in list_result.text

    unset_result = _run_cli(["cron", "service", "env", "unset", "FEISHU_APP_SECRET"])
    assert unset_result.exit_code == 0
    assert "Unset service env FEISHU_APP_SECRET" in unset_result.text
    assert "launchd" in unset_result.text


def test_cron_service_env_set_invalid_key_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    result = _run_cli(["cron", "service", "env", "set", "feishu-secret", "secret"])

    assert result.exit_code == 2
    assert "invalid service env key" in result.text


def test_cron_doctor_warns_active_feishu_job_missing_service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")

    import gateway.registry as gateway_registry
    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from gateway.contracts import SendResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(True)

        def send_text(self, target, message):
            raise AssertionError("doctor must not send a Feishu message")

        def token_smoke(self):
            return SendResult(True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")
        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 2
    assert "service env missing FEISHU_APP_ID, FEISHU_APP_SECRET" in result.text
    assert "current shell Feishu env is set but cron service env is missing" in result.text
    assert "cron service env set FEISHU_APP_ID" in result.text


def test_cron_doctor_reports_service_env_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    env_file = tmp_path / "cron" / "service.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FEISHU_APP_ID=cli_123\n", encoding="utf-8")
    env_file.chmod(0o644)

    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)

    result = cron_doctor()

    assert "service env permissions are too broad" in result.text


def test_cron_doctor_warns_installed_systemd_unit_missing_runtime_context(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text(
        "[Service]\nExecStart=/usr/bin/python -m agent_cli.main cron serve\n",
        encoding="utf-8",
    )

    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: True)

    result = cron_doctor()

    assert "cron service definition missing WorkingDirectory" in result.text
    assert "cron service definition missing project PYTHONPATH" in result.text
    assert "cron service definition missing service.env reference" in result.text
    assert "cron service install --force" in result.text


def test_cron_doctor_warns_launchd_plist_has_stale_service_env(monkeypatch, tmp_path):
    import plistlib

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    env_file = tmp_path / "home" / "cron" / "service.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "FEISHU_APP_ID=new-app\nFEISHU_APP_SECRET=secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    project_root = tmp_path / "repo"
    plist = plistlib.dumps(
        {
            "WorkingDirectory": str(project_root),
            "EnvironmentVariables": {
                "PYTHONPATH": str(project_root),
                "FEISHU_APP_ID": "old-app",
                "FEISHU_APP_SECRET": "secret",
            },
        }
    )

    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.service_context import ServiceRuntimeContext
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_context.build_service_runtime_context",
        lambda: ServiceRuntimeContext(
            project_root=project_root,
            working_directory=project_root,
            pythonpath=str(project_root),
            service_env_file=env_file,
        ),
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="launchd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user.read_installed_plist", lambda: plist)
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: True)

    result = cron_doctor()

    assert "launchd service env is stale for FEISHU_APP_ID" in result.text
    assert "cron service install --force" in result.text


def test_cron_run_uses_scheduler_path_and_records_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client

    run_time = datetime.fromisoformat("2026-06-02T18:10:00+08:00")
    state_store.utc_now = lambda: run_time

    job = create_job("manual cli output", "every 5m", name="Manual CLI", deliver="local")

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="manual cli output",
            final_response="manual cli response",
            error=None,
            exit_reason=None,
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Run:" in result.text
    assert "Status: succeeded" in result.text or "Status: ok" in result.text
    assert "Output:" in result.text
    runs = _run_cli(["cron", "runs", job["id"]])
    assert "status=succeeded" in runs.text


def test_cron_run_reports_run_specific_delivery_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.delivery_registry as delivery_registry
    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client

    run_time = datetime.fromisoformat("2026-06-02T18:10:00+08:00")
    state_store.utc_now = lambda: run_time

    job = create_job(
        "manual webhook output",
        "every 5m",
        name="Manual webhook",
        deliver="webhook:https://example.invalid/hook",
    )

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="manual webhook output",
            final_response="manual webhook response",
            error=None,
            exit_reason=None,
        )

    def failing_registry(*, webhook_sender=None):
        return delivery_registry.build_delivery_registry(
            webhook_sender=lambda url, payload, timeout=10: (500, "down")
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)
    monkeypatch.setattr(delivery_registry, "default_delivery_registry", failing_registry)

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Delivery: delivered=0 failed=1 pending=0 dead=0" in result.text
    assert "Inspect delivery: python -m agent_cli cron deliveries" in result.text


def test_cron_run_does_not_report_unrelated_delivery_maintenance(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client
    from cron.state_store import StateStore

    run_time = datetime.fromisoformat("2026-06-02T18:10:00+08:00")
    state_store.utc_now = lambda: run_time
    store = StateStore()
    store.enqueue_delivery_event(
        job_id="other-job",
        run_id="other-run",
        job_name="Other",
        run_at="2026-06-02T18:09:00+08:00",
        target="local",
        target_type="local",
        adapter_key="local",
        address=None,
        thread_id=None,
        origin=None,
        final_response="other",
        output_path=None,
        payload={"message": "other"},
        status="pending",
    )
    job = create_job("manual local output", "every 5m", name="Manual local", deliver="local")

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="manual local output",
            final_response="manual local response",
            error=None,
            exit_reason=None,
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Delivery tick:" not in result.text
    assert "Delivery: delivered=1 failed=0 pending=0 dead=0" in result.text


def test_cron_run_skipped_run_does_not_report_unrelated_delivery_maintenance(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.state_store as state_store
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    run_time = datetime.fromisoformat("2026-06-02T18:10:00+08:00")
    state_store.utc_now = lambda: run_time
    store = StateStore()
    job = create_job("manual skip", "every 5m", name="Manual skip", deliver="local")
    update_job(
        job["id"],
        {
            "concurrency_key": "manual-skip-key",
            "concurrency_policy": "skip_if_running",
        },
    )
    store.claim_manual_job(job["id"], now_text="2026-06-02T18:09:00+08:00")
    store.enqueue_delivery_event(
        job_id="other-job",
        run_id="other-run",
        job_name="Other",
        run_at="2026-06-02T18:09:00+08:00",
        target="local",
        target_type="local",
        adapter_key="local",
        address=None,
        thread_id=None,
        origin=None,
        final_response="other",
        output_path=None,
        payload={"message": "other"},
        status="pending",
    )

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Status: skipped" in result.text
    assert "Delivery: -" in result.text
    assert "Delivery tick:" not in result.text


def test_cron_run_dry_run_reports_manual_schedule_preservation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.state_store as state_store
    from cron.jobs import create_job

    run_time = datetime.fromisoformat("2026-06-02T18:10:00+08:00")
    state_store.utc_now = lambda: run_time

    job = create_job("manual dry", "every 5m", name="Manual Dry", deliver="local")

    result = _run_cli(["cron", "run", job["id"], "--dry-run"])

    assert result.exit_code == 0
    assert "Manual run: yes" in result.text
    assert "Preserves periodic schedule: yes" in result.text
    assert "Decision: would_claim" in result.text


def test_cron_doctor_warns_when_prod_expects_docker_but_command_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    import cron.docker_diagnostics as docker_diagnostics

    monkeypatch.setattr(
        docker_diagnostics,
        "inspect_docker_runtime",
        lambda profile: docker_diagnostics.DockerRuntimeDiagnostic(
            expected=True,
            env_type="docker",
            command_path=None,
            version_ok=False,
            error="docker command not found",
            suggestion="Install Docker or set TERMINAL_ENV=local for development.",
        ),
    )

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "docker runtime: docker command not found" in result.text
    assert "Install Docker or set TERMINAL_ENV=local for development." in result.text


def test_cron_doctor_warns_when_docker_version_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    import cron.docker_diagnostics as docker_diagnostics

    monkeypatch.setattr(
        docker_diagnostics,
        "inspect_docker_runtime",
        lambda profile: docker_diagnostics.DockerRuntimeDiagnostic(
            expected=True,
            env_type="docker",
            command_path="/usr/bin/docker",
            version_ok=False,
            error="Docker command is available but 'docker version' failed.",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        ),
    )

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "docker runtime: Docker command is available but 'docker version' failed." in result.text
    assert "Start Docker or fix permission to access the Docker daemon." in result.text


def test_cron_doctor_respects_explicit_local_terminal_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    result = _run_cli(["cron", "doctor"])

    assert "docker runtime: not required for TERMINAL_ENV=local" in result.text
    assert "docker command not found" not in result.text


def test_cron_doctor_feishu_service_env_includes_restart_command(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")
    from cron.jobs import create_job

    create_job("send", "every 5m", name="Feishu active", deliver="feishu:oc_1234567890abcdef")

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 2
    assert "agent cron service env set FEISHU_APP_ID APP_ID_VALUE" in result.text
    assert "agent cron service env set FEISHU_APP_SECRET APP_SECRET_VALUE" in result.text
    assert "agent cron service restart" in result.text
    assert "background service delivery uses service.env" in result.text


def test_cron_doctor_warns_overdue_next_due_with_stale_service(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job
    from cron.state_store import StateStore
    import agent_cli.cron_commands as cron_commands

    job = create_job("overdue", "every 5m", name="Overdue", deliver="local")
    store = StateStore()
    saved = store.get_job(job["id"])
    saved["next_run_at"] = "2026-06-02T18:00:00+08:00"
    store.update_job(saved["id"], saved)

    monkeypatch.setattr(
        cron_commands,
        "_service_status_lines",
        lambda: ["Heartbeat: stale-or-missing"],
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "next due job is overdue" in result.text
    assert "agent cron service start" in result.text
    assert "agent cron service restart" in result.text


def test_cron_doctor_warns_active_job_missing_next_run_at(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job("missing next", "every 5m", name="Missing next", deliver="local")
    store = StateStore()
    saved = store.get_job(job["id"])
    saved["next_run_at"] = None
    store.update_job(saved["id"], saved)

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "active cron jobs missing next_run_at" in result.text
    assert job["id"] in result.text
    assert "agent cron run" in result.text


def test_cron_doctor_warns_recent_missed_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.state_store import StateStore

    run_time = datetime.fromisoformat("2026-06-02T19:00:00+08:00")
    state_store.utc_now = lambda: run_time

    job = create_job("missed", "every 5m", name="Missed", deliver="local")
    store = StateStore()
    saved = store.get_job(job["id"])
    saved["next_run_at"] = "2026-06-02T18:00:00+08:00"
    store.update_job(saved["id"], saved)
    store.claim_due_jobs(now_text="2026-06-02T19:00:00+08:00", limit=1)

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "recent missed cron runs" in result.text
    assert f"agent cron runs {job['id']}" in result.text
