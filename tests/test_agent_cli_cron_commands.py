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


def test_cron_doctor_reports_fresh_service_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.jobs import now
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
    write_service_status(
        {
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": now().isoformat(),
        }
    )

    result = cron_commands.cron_doctor()

    assert "[ok] cron service heartbeat: fresh (leader)" in result.text
    assert "scheduler is stopped" not in result.text


def test_cron_doctor_warns_when_service_status_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert (
        "[warn] cron service heartbeat: missing; start automatic scheduling "
        "with `agent cron serve`"
    ) in result.text


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
    from cron.service_state import write_service_status

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
    assert "runner_worker entrypoint: resolvable" in result.text
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
    assert "runner_worker entrypoint failed" in result.text
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


def test_cron_doctor_lists_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.text
    assert "local" in result.text
    assert "origin" in result.text
    assert "webhook" in result.text
