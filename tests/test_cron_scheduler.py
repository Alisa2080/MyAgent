from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest


RUN_AT = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)


def test_tick_claims_run_and_completes_without_pre_advance(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore()
    calls = []

    fake_runner = lambda claimed_job: (
        calls.append(("run", claimed_job["id"], claimed_job.get("run_id")))
        or JobRunResult(True, "doc", "final", None)
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: calls.append(("save", job_id, doc)) or str(tmp_path / "out.md"),
    )
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    runs = store.runs_for_job(job["id"])
    assert result.due == 1
    assert result.ran == 1
    assert runs[0]["status"] == "succeeded"
    assert calls[0][0] == "run"
    assert store.get_job(job["id"])["state"] in {"scheduled", "completed"}


def test_default_runner_import_failure_marks_job_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore()

    monkeypatch.setattr(
        scheduler,
        "_default_job_runner",
        lambda: (_ for _ in ()).throw(ImportError("runner unavailable")),
    )
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    result = scheduler.tick(now_dt=RUN_AT)

    runs = store.runs_for_job(job["id"])
    assert result.due == 1
    assert result.ran == 1
    assert result.failed == 1
    assert "runner unavailable" in str(result.results[0].error)
    assert runs[0]["status"] == "failed"
    assert store.get_job(job["id"])["state"] == "completed"
    assert store.get_job(job["id"])["next_run_at"] is None


def test_runner_exception_advances_recurring_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore()
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    result = scheduler.tick(
        now_dt=RUN_AT,
        job_runner=lambda claimed_job: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    stored_job = store.get_job(job["id"])
    assert result.failed == 1
    assert store.runs_for_job(job["id"])[0]["status"] == "failed"
    assert stored_job["state"] == "scheduled"
    assert stored_job["next_run_at"] == "2026-05-22T09:30:00+00:00"
    assert stored_job["next_run_at"] != RUN_AT.isoformat()


def test_lost_lease_skips_output_and_delivery(monkeypatch, tmp_path):
    """When a run's lease expires before mark_run_started, the run is abandoned."""
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []
    saved = []

    from datetime import timedelta
    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    import cron.state_store as state_store
    from cron.state_store import StateStore

    current = [RUN_AT]

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore(lease_seconds=1)

    def runner(claimed_job):
        current[0] = current[0] + timedelta(seconds=2)
        store.recover_expired_leases(now_text=current[0].isoformat())
        return JobRunResult(True, "doc", "final", None)

    monkeypatch.setattr(scheduler, "_store", lambda: StateStore(lease_seconds=1))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: saved.append((job_id, doc)) or str(tmp_path / "out.md"))
    monkeypatch.setattr(state_store, "utc_now", lambda: current[0])
    from cron import delivery
    monkeypatch.setattr(delivery, "default_webhook_sender", lambda url, payload, timeout=10: sent.append(url) or (204, "ok"))

    result = scheduler.tick(now_dt=RUN_AT, job_runner=runner)

    runs = store.runs_for_job(job["id"])
    assert result.failed == 1
    assert "lease" in result.results[0].error
    assert saved == []
    assert sent == []
    assert DeliveryStore().stats()["pending"] == 0
    assert runs[0]["status"] == "abandoned"


def test_expired_unrecovered_lease_skips_output_and_delivery(monkeypatch, tmp_path):
    """When a run's lease expires before mark_run_started (unrecovered), the run is abandoned."""
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    saved = []
    sent = []

    from datetime import datetime, timedelta, timezone

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    import cron.state_store as state_store
    from cron.state_store import StateStore

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    current = [datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)]
    later = current[0] + timedelta(minutes=1)
    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": base.isoformat()})
    store = StateStore(lease_seconds=1)

    monkeypatch.setattr(scheduler, "_store", lambda: StateStore(lease_seconds=1))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: saved.append((job_id, doc)) or str(tmp_path / "out.md"))
    monkeypatch.setattr(state_store, "utc_now", lambda: current[0])
    from cron import delivery
    monkeypatch.setattr(delivery, "default_webhook_sender", lambda url, payload, timeout=10: sent.append(url) or (204, "ok"))

    def runner(claimed_job):
        current[0] = later
        return JobRunResult(True, "doc", "final", None)

    result = scheduler.tick(now_dt=base, job_runner=runner)

    assert result.failed == 1
    assert "lease" in result.results[0].error
    assert saved == []
    assert sent == []
    assert DeliveryStore().stats()["pending"] == 0
    assert store.runs_for_job(job["id"])[0]["status"] == "abandoned"
    stored_job = store.get_job(job["id"])
    assert stored_job["state"] == "scheduled"
    assert stored_job["lease_run_id"] is None
    assert stored_job["lease_expires_at"] is None


def test_lease_loss_after_enqueue_deletes_pending_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    saved = []

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.delivery as delivery_module
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore(lease_seconds=1)
    real_enqueue = delivery_module.enqueue_result

    def enqueue_then_lose_lease(*args, **kwargs):
        events = real_enqueue(*args, **kwargs)
        store.recover_expired_leases(now_text="2100-01-01T00:00:00+00:00")
        return events

    monkeypatch.setattr(scheduler, "_store", lambda: StateStore(lease_seconds=1))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: saved.append((job_id, doc)) or str(tmp_path / "out.md"))
    monkeypatch.setattr(delivery_module, "enqueue_result", enqueue_then_lose_lease)
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    result = scheduler.tick(now_dt=RUN_AT, job_runner=lambda claimed_job: JobRunResult(True, "doc", "final", None))

    assert result.failed == 1
    assert result.results[0].error == "run lease lost before dispatch"
    assert saved == [(job["id"], "doc")]
    assert DeliveryStore().stats()["pending"] == 0


def test_tick_queues_origin_notification(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="origin", origin={"thread_id": "thread-1"})
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "final", None)
    scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    store = DeliveryStore()
    stats = store.stats()
    assert stats["pending"] == 1
    events = store.pending_origin_events("thread-1")
    assert len(events) == 1
    assert events[0]["job_id"] == job["id"]


def test_jobs_facade_rejects_unsupported_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job

    with pytest.raises(ValueError, match="unsupported delivery target: telegram:123"):
        create_job(prompt="daily report", schedule="30m", deliver="telegram:123", origin={"thread_id": "thread-1"})


def test_migrated_legacy_delivery_error_is_persisted_with_real_job_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import update_job
    from cron.state_store import StateStore
    import cron.scheduler as scheduler

    delivery_error = "unsupported delivery target: telegram:123"
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-job",
                        "name": "daily report",
                        "prompt": "daily report",
                        "schedule": {"kind": "interval", "minutes": 30},
                        "schedule_display": "every 30m",
                        "enabled": True,
                        "state": "scheduled",
                        "next_run_at": RUN_AT.isoformat(),
                        "last_run_at": None,
                        "last_status": None,
                        "last_error": None,
                        "last_delivery_error": None,
                        "repeat": {"times": None, "completed": 0},
                        "deliver": "telegram:123",
                        "origin": {"thread_id": "thread-1"},
                        "workdir": None,
                        "script": None,
                        "context_from": None,
                        "skills": [],
                        "enabled_toolsets": None,
                        "model": None,
                        "provider": None,
                        "base_url": None,
                        "created_at": RUN_AT.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    job = StateStore().get_job("legacy-job")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "final", None)
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    from cron.jobs import get_job
    persisted = get_job(job["id"])
    assert result.due == 1
    assert result.succeeded == 1
    assert result.results[0].error == delivery_error
    assert persisted["last_status"] == "ok"
    assert persisted["last_error"] is None
    assert persisted["last_delivery_error"] == delivery_error


def test_silent_response_suppresses_origin_notification(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="origin", origin={"thread_id": "thread-1"})
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "[SILENT] nothing changed", None)
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.succeeded == 1
    assert DeliveryStore().stats()["pending"] == 0


def test_failed_run_saves_marks_failed_and_enqueues_error_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="origin", origin={"thread_id": "thread-1"})
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: JobRunResult(False, "failure doc", "", "boom")
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.failed == 1
    assert result.results[0].error == "boom"
    events = DeliveryStore().pending_origin_events("thread-1")
    assert len(events) == 1
    assert events[0]["job_id"] == job["id"]
    assert '"status": "error"' in events[0]["payload_json"]


def test_webhook_failure_is_recorded_as_delivery_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    from cron import delivery
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    monkeypatch.setattr(delivery, "default_webhook_sender", lambda url, payload, timeout=10: (500, "server down"))

    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "final", None)
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.results[0].error == "HTTP 500: server down"


def test_workdir_jobs_run_sequentially(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    order = []
    workdir_a = tmp_path / "workdir_a"
    workdir_b = tmp_path / "workdir_b"
    workdir_a.mkdir()
    workdir_b.mkdir()
    job_a = create_job(prompt="job a", schedule="30m", name="a", deliver="local", workdir=str(workdir_a))
    job_b = create_job(prompt="job b", schedule="30m", name="b", deliver="local", workdir=str(workdir_b))
    update_job(job_a["id"], {"next_run_at": RUN_AT.isoformat()})
    update_job(job_b["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: (
        order.append(claimed_job["id"])
        or JobRunResult(True, "doc", "final", None)
    )
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.ran == 2
    assert set(order) == {job_a["id"], job_b["id"]}


def test_non_workdir_jobs_use_parallel_executor_and_env_max(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job_a = create_job(prompt="job a", schedule="30m", name="a", deliver="local")
    job_b = create_job(prompt="job b", schedule="30m", name="b", deliver="local")
    job_c = create_job(prompt="job c", schedule="30m", name="c", deliver="local")
    update_job(job_a["id"], {"next_run_at": RUN_AT.isoformat()})
    update_job(job_b["id"], {"next_run_at": RUN_AT.isoformat()})
    update_job(job_c["id"], {"next_run_at": RUN_AT.isoformat()})

    executor_calls = []

    class FakeFuture:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    class FakeExecutor:
        def __init__(self, max_workers):
            executor_calls.append(("init", max_workers))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            executor_calls.append(("exit",))

        def submit(self, fn, claimed):
            executor_calls.append(("submit", claimed["job"]["id"]))
            return FakeFuture(fn(claimed))

    monkeypatch.setenv("AGENT_CRON_MAX_PARALLEL", "2")
    monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "final", None)
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.ran == 3
    assert executor_calls[0] == ("init", 2)
    assert set(executor_calls[1:4]) == {("submit", job_a["id"]), ("submit", job_b["id"]), ("submit", job_c["id"])}
    assert ("exit",) in executor_calls


def test_invalid_env_parallel_uses_due_job_count(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job_a = create_job(prompt="job a", schedule="30m", name="a", deliver="local")
    job_b = create_job(prompt="job b", schedule="30m", name="b", deliver="local")
    update_job(job_a["id"], {"next_run_at": RUN_AT.isoformat()})
    update_job(job_b["id"], {"next_run_at": RUN_AT.isoformat()})

    workers_seen = []

    class FakeExecutor:
        def __init__(self, max_workers):
            workers_seen.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def submit(self, fn, claimed):
            class Future:
                def result(self):
                    return fn(claimed)
            return Future()

    monkeypatch.setenv("AGENT_CRON_MAX_PARALLEL", "not-an-int")
    monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)
    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    fake_runner = lambda claimed_job: JobRunResult(True, "doc", "final", None)
    scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert workers_seen == [2]


def test_lock_busy_returns_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.scheduler import fcntl
    import cron.scheduler as scheduler

    if fcntl is None:
        pytest.skip("fcntl lock behavior is Unix-specific")

    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    with lock_file.open("a+") as handle:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
        try:
            result = scheduler.tick(now_dt=RUN_AT)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert result == scheduler.TickResult(skipped=1)


def test_blocking_io_error_after_lock_acquisition_propagates(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.scheduler import _store, _TickLock, _TickLockBusy, TickResult
    from cron.state_store import StateStore

    store = StateStore()

    def raise_blocking(now_text=None, limit=100):
        raise BlockingIOError("storage busy")

    original = store.claim_due_jobs
    store.claim_due_jobs = raise_blocking

    import cron.scheduler as scheduler
    monkeypatch.setattr(scheduler, "_store", lambda: store)

    with pytest.raises(BlockingIOError, match="storage busy"):
        scheduler.tick(now_dt=RUN_AT)


def test_tick_enqueues_delivery_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler

    job = create_job(prompt="write report", schedule="30m", name="Daily", deliver="origin", origin={"thread_id": "thread-1"})
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)
    fake_runner = lambda claimed_job: JobRunResult(success=True, output_doc="# out", final_response="done")
    result = scheduler.tick(now_dt=RUN_AT, job_runner=fake_runner)

    assert result.ran == 1
    assert DeliveryStore().stats()["pending"] == 1


def test_processing_exception_marks_error_and_returns_failed_result(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore()

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: RUN_AT)

    def bad_runner(claimed_job):
        raise RuntimeError("bad runner error")

    result = scheduler.tick(now_dt=RUN_AT, job_runner=bad_runner)

    runs = store.runs_for_job(job["id"])
    assert result.failed == 1
    assert result.results[0].error == "bad runner error"
    assert runs[0]["status"] == "failed"


def test_tick_records_run_and_advances_after_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    due_at = "2026-05-30T10:00:00+00:00"
    from datetime import datetime, timezone
    due_dt = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)

    job = create_job(prompt="write report", schedule="every 30m", deliver="local")
    update_job(job["id"], {"next_run_at": due_at})

    def fake_runner(running_job):
        assert running_job["run_id"]
        assert running_job["next_run_at"] == due_at
        return JobRunResult(success=True, output_doc="# out", final_response="done")

    import cron.state_store as state_store
    monkeypatch.setattr(state_store, "utc_now", lambda: due_dt)

    result = scheduler.tick(now_dt=due_dt, job_runner=fake_runner)

    store = StateStore()
    runs = store.runs_for_job(job["id"])
    job_after = store.get_job(job["id"])
    assert result.ran == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["scheduled_for"] == due_at
    assert job_after["state"] == "scheduled"
    assert job_after["next_run_at"] != due_at
    assert job_after["lease_run_id"] is None
