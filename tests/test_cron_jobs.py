from datetime import datetime, timedelta, timezone
import sys

import pytest


def import_jobs_module():
    try:
        import cron.jobs as jobs

        return jobs
    except ModuleNotFoundError as exc:
        # cron/__init__.py still imports the legacy scheduler in this task.
        if exc.name == "hermes_constants" and "cron.jobs" in sys.modules:
            return sys.modules["cron.jobs"]
        raise


def test_parse_duration_schedule_creates_oneshot(monkeypatch, tmp_path):
    jobs = import_jobs_module()

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: base)

    parsed = jobs.parse_schedule("30m")

    assert parsed["kind"] == "once"
    assert parsed["run_at"] == (base + timedelta(minutes=30)).isoformat()


def test_create_job_defaults_origin_delivery(monkeypatch, tmp_path):
    jobs = import_jobs_module()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        jobs,
        "now",
        lambda: datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc),
    )

    job = jobs.create_job(
        prompt="write a report",
        schedule="30m",
        origin={"thread_id": "thread-1"},
    )

    assert job["deliver"] == "origin"
    assert job["repeat"] == {"times": 1, "completed": 0}
    assert jobs.get_job(job["id"])["name"] == "write a report"


def test_workdir_must_be_absolute(monkeypatch, tmp_path):
    jobs = import_jobs_module()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="absolute path"):
        jobs.create_job(prompt="x", schedule="30m", workdir="relative/path")


def test_get_due_jobs_fast_forwards_stale_interval(monkeypatch, tmp_path):
    jobs = import_jobs_module()

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: base)
    job = jobs.create_job(prompt="x", schedule="every 10m", deliver="local")
    jobs.update_job(
        job["id"],
        {"next_run_at": (base - timedelta(hours=3)).isoformat()},
    )

    due = jobs.get_due_jobs(now_dt=base)

    assert due == []
    refreshed = jobs.get_job(job["id"])
    assert datetime.fromisoformat(refreshed["next_run_at"]) > base


def test_advance_next_run_before_mark_run(monkeypatch, tmp_path):
    jobs = import_jobs_module()

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: base)
    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": base.isoformat()})

    advanced = jobs.advance_next_run(job["id"], base)
    jobs.mark_job_run(job["id"], success=True, error=None, run_at=base)

    assert advanced["next_run_at"] == (base + timedelta(minutes=30)).isoformat()
    final = jobs.get_job(job["id"])
    assert final["last_status"] == "ok"
    assert final["repeat"]["completed"] == 1
