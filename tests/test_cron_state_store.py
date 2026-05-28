from __future__ import annotations

import json


def test_state_store_initializes_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()

    assert store.path.name == "cron.sqlite3"
    assert store.schema_version() == 1
    assert store.job_counts_by_state() == {}
    assert store.delivery_stats()["pending"] == 0


def test_state_store_imports_jobs_json_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    jobs_file = cron_dir / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "daily",
                        "prompt": "write report",
                        "schedule": {"kind": "interval", "minutes": 60},
                        "schedule_display": "every 60m",
                        "enabled": True,
                        "state": "scheduled",
                        "next_run_at": "2026-05-28T10:00:00+00:00",
                        "last_run_at": None,
                        "last_status": None,
                        "last_error": None,
                        "repeat": {"times": None, "completed": 0},
                        "deliver": "origin",
                        "origin": {"thread_id": "thread-1"},
                        "workdir": None,
                        "script": None,
                        "context_from": None,
                        "skills": ["reports"],
                        "skill": "reports",
                        "enabled_toolsets": None,
                        "model": None,
                        "provider": None,
                        "base_url": None,
                        "created_at": "2026-05-28T09:00:00+00:00",
                    }
                ],
                "updated_at": "2026-05-28T09:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    from cron.state_store import StateStore

    store = StateStore()
    jobs = store.list_jobs(include_disabled=True)

    assert [job["id"] for job in jobs] == ["job-1"]
    assert jobs[0]["origin"] == {"thread_id": "thread-1"}
    assert jobs[0]["skills"] == ["reports"]
    assert store.get_meta("jobs_json_imported_at")
    before = jobs_file.read_text(encoding="utf-8")

    store.create_job(
        {
            "id": "job-2",
            "name": "new",
            "prompt": "new prompt",
            "schedule": {"kind": "once", "run_at": "2026-05-28T11:00:00+00:00"},
            "schedule_display": "once",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-05-28T11:00:00+00:00",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "repeat": {"times": 1, "completed": 0},
            "deliver": "local",
            "origin": None,
            "workdir": None,
            "script": None,
            "context_from": None,
            "skills": [],
            "skill": None,
            "enabled_toolsets": None,
            "model": None,
            "provider": None,
            "base_url": None,
            "created_at": "2026-05-28T09:05:00+00:00",
        }
    )

    assert jobs_file.read_text(encoding="utf-8") == before
    assert {job["id"] for job in store.list_jobs(include_disabled=True)} == {"job-1", "job-2"}


def test_jobs_facade_create_update_pause_resume_remove(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, get_job, list_jobs, pause_job, remove_job, resume_job, update_job

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")

    assert job["id"]
    assert job["state"] == "scheduled"
    assert list_jobs()[0]["id"] == job["id"]

    updated = update_job(job["id"], {"name": "daily updated"})
    assert updated["name"] == "daily updated"

    paused = pause_job(job["id"], reason="test")
    assert paused["state"] == "paused"
    assert paused["enabled"] is False
    assert list_jobs() == []
    assert get_job(job["id"])["paused_reason"] == "test"

    resumed = resume_job(job["id"])
    assert resumed["state"] == "scheduled"
    assert resumed["enabled"] is True

    assert remove_job(job["id"]) is True
    assert get_job(job["id"]) is None
