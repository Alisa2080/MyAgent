from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace


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
    assert "Delivery adapters: local, origin, webhook" in result.text


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
    assert result.exit_code == 0


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
