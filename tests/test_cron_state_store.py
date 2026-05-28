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
