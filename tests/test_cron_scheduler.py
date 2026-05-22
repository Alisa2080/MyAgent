from __future__ import annotations

from datetime import datetime, timezone

import pytest


RUN_AT = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)


def test_tick_advances_before_run_and_marks_result(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    calls = []
    job = {"id": "job-1", "name": "daily", "workdir": None, "deliver": "local"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(
        scheduler,
        "advance_next_run",
        lambda job_id, run_at: calls.append(("advance", job_id, run_at)) or job,
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: calls.append(("run", advanced["id"]))
        or scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: calls.append(("save", job_id, doc, run_at))
        or "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: (
            calls.append(("mark", job_id, success, error, run_at, delivery_error))
        ),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.due == 1
    assert result.ran == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.results == [
        scheduler.JobTickResult(job_id="job-1", success=True, output_path="/tmp/out.md")
    ]
    assert calls == [
        ("advance", "job-1", RUN_AT),
        ("run", "job-1"),
        ("save", "job-1", "doc", RUN_AT),
        ("mark", "job-1", True, None, RUN_AT, None),
    ]


def test_tick_queues_origin_notification(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    job = {
        "id": "job-1",
        "name": "daily",
        "workdir": None,
        "deliver": "origin",
        "origin": {"thread_id": "thread-1"},
    }
    queued = []

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: None,
    )
    monkeypatch.setattr(
        scheduler,
        "queue_cron_notification",
        lambda thread_id, event: queued.append((thread_id, event)),
    )

    scheduler.tick(now_dt=RUN_AT)

    assert queued == [
        (
            "thread-1",
            {
                "type": "cron_result",
                "job_id": "job-1",
                "job_name": "daily",
                "status": "ok",
                "final_response": "final",
                "output_path": "/tmp/out.md",
                "error": None,
                "run_at": RUN_AT.isoformat(),
            },
        )
    ]


def test_legacy_delivery_value_runs_without_origin_notification(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    calls = []
    delivery_error = "Unsupported delivery target: telegram:123"
    job = {
        "id": "job-1",
        "name": "daily",
        "workdir": None,
        "deliver": "telegram:123",
        "origin": {"thread_id": "thread-1"},
    }

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: calls.append(("save", job_id, doc, run_at))
        or "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: (
            calls.append(("mark", job_id, success, error, run_at, delivery_error))
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "queue_cron_notification",
        lambda thread_id, event: calls.append(("notify", thread_id, event)),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.due == 1
    assert result.ran == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.results == [
        scheduler.JobTickResult(
            job_id="job-1",
            success=True,
            output_path="/tmp/out.md",
            error=delivery_error,
        )
    ]
    assert calls == [
        ("save", "job-1", "doc", RUN_AT),
        ("mark", "job-1", True, None, RUN_AT, delivery_error),
    ]


def test_legacy_delivery_error_is_persisted_with_real_job_storage(monkeypatch, tmp_path):
    import cron.jobs as jobs
    import cron.scheduler as scheduler

    delivery_error = "Unsupported delivery target: telegram:123"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(
        prompt="daily report",
        schedule="30m",
        deliver="telegram:123",
        origin={"thread_id": "thread-1"},
    )
    jobs.update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "queue_cron_notification",
        lambda thread_id, event: pytest.fail("unsupported delivery must not notify"),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    persisted = jobs.get_job(job["id"])
    assert result.due == 1
    assert result.succeeded == 1
    assert result.results == [
        scheduler.JobTickResult(
            job_id=job["id"],
            success=True,
            output_path=str(
                tmp_path / "cron" / "output" / job["id"] / "20260522_090000.md"
            ),
            error=delivery_error,
        )
    ]
    assert persisted["last_status"] == "ok"
    assert persisted["last_error"] is None
    assert persisted["last_delivery_error"] == delivery_error


def test_silent_response_suppresses_origin_notification(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    job = {
        "id": "job-1",
        "name": "daily",
        "workdir": None,
        "deliver": "origin",
        "origin": {"thread_id": "thread-1"},
    }
    queued = []

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: scheduler.JobRunResult(
            True,
            "doc",
            "[SILENT] nothing changed",
            None,
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: None,
    )
    monkeypatch.setattr(
        scheduler,
        "queue_cron_notification",
        lambda thread_id, event: queued.append((thread_id, event)),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.succeeded == 1
    assert queued == []


def test_failed_run_saves_marks_failed_and_does_not_notify(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    calls = []
    job = {
        "id": "job-1",
        "name": "daily",
        "workdir": None,
        "deliver": "origin",
        "origin": {"thread_id": "thread-1"},
    }

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: scheduler.JobRunResult(False, "failure doc", "", "boom"),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: calls.append(("save", job_id, doc, run_at))
        or "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: (
            calls.append(("mark", job_id, success, error, run_at, delivery_error))
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "queue_cron_notification",
        lambda thread_id, event: calls.append(("notify", thread_id, event)),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.failed == 1
    assert result.results == [
        scheduler.JobTickResult(
            job_id="job-1",
            success=False,
            output_path="/tmp/out.md",
            error="boom",
        )
    ]
    assert calls == [
        ("save", "job-1", "failure doc", RUN_AT),
        ("mark", "job-1", False, "boom", RUN_AT, None),
    ]


def test_workdir_jobs_run_sequentially(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    order = []
    jobs = [
        {"id": "a", "name": "a", "workdir": "/tmp/a", "deliver": "local"},
        {"id": "b", "name": "b", "workdir": "/tmp/b", "deliver": "local"},
    ]

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: jobs)
    monkeypatch.setattr(
        scheduler,
        "advance_next_run",
        lambda job_id, run_at: next(job for job in jobs if job["id"] == job_id),
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job: order.append(job["id"])
        or scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: None,
    )

    scheduler.tick(now_dt=RUN_AT)

    assert order == ["a", "b"]


def test_non_workdir_jobs_use_parallel_executor_and_env_max(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    jobs = [
        {"id": "a", "name": "a", "workdir": None, "deliver": "local"},
        {"id": "b", "name": "b", "workdir": None, "deliver": "local"},
        {"id": "c", "name": "c", "workdir": None, "deliver": "local"},
    ]
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

        def submit(self, fn, job, run_at):
            executor_calls.append(("submit", job["id"], run_at))
            return FakeFuture(fn(job, run_at))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "2")
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: jobs)
    monkeypatch.setattr(
        scheduler,
        "advance_next_run",
        lambda job_id, run_at: next(job for job in jobs if job["id"] == job_id),
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job: scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: None,
    )
    monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.ran == 3
    assert executor_calls == [
        ("init", 2),
        ("submit", "a", RUN_AT),
        ("submit", "b", RUN_AT),
        ("submit", "c", RUN_AT),
        ("exit",),
    ]


def test_invalid_env_parallel_uses_due_job_count(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    jobs = [
        {"id": "a", "name": "a", "workdir": None, "deliver": "local"},
        {"id": "b", "name": "b", "workdir": None, "deliver": "local"},
    ]
    workers_seen = []

    class FakeExecutor:
        def __init__(self, max_workers):
            workers_seen.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def submit(self, fn, job, run_at):
            class Future:
                def result(self):
                    return fn(job, run_at)

            return Future()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "not-an-int")
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: jobs)
    monkeypatch.setattr(
        scheduler,
        "advance_next_run",
        lambda job_id, run_at: next(job for job in jobs if job["id"] == job_id),
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job: scheduler.JobRunResult(True, "doc", "final", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: None,
    )
    monkeypatch.setattr(
        scheduler.concurrent.futures,
        "ThreadPoolExecutor",
        FakeExecutor,
    )

    scheduler.tick(now_dt=RUN_AT)

    assert workers_seen == [2]


def test_lock_busy_returns_skipped(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    if scheduler.fcntl is None:
        pytest.skip("fcntl lock behavior is Unix-specific")

    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    with lock_file.open("a+") as handle:
        scheduler.fcntl.flock(
            handle.fileno(),
            scheduler.fcntl.LOCK_EX | scheduler.fcntl.LOCK_NB,
        )
        try:
            result = scheduler.tick(now_dt=RUN_AT)
        finally:
            scheduler.fcntl.flock(handle.fileno(), scheduler.fcntl.LOCK_UN)

    assert result == scheduler.TickResult(skipped=1)


def test_blocking_io_error_after_lock_acquisition_propagates(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "get_due_jobs",
        lambda now_dt=None: (_ for _ in ()).throw(BlockingIOError("storage busy")),
    )

    with pytest.raises(BlockingIOError, match="storage busy"):
        scheduler.tick(now_dt=RUN_AT)


def test_processing_exception_marks_error_and_returns_failed_result(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    calls = []
    job = {"id": "job-1", "name": "daily", "workdir": None, "deliver": "local"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(
        scheduler,
        "advance_next_run",
        lambda job_id, run_at: (_ for _ in ()).throw(RuntimeError("bad advance")),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None: calls.append(
            ("mark", job_id, success, error, run_at)
        ),
    )

    result = scheduler.tick(now_dt=RUN_AT)

    assert result.failed == 1
    assert result.results == [
        scheduler.JobTickResult(
            job_id="job-1",
            success=False,
            error="bad advance",
        )
    ]
    assert calls == [("mark", "job-1", False, "bad advance", RUN_AT)]
