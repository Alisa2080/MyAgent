from __future__ import annotations

import json


def test_state_store_initializes_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()

    assert store.path.name == "cron.sqlite3"
    assert store.schema_version() == 2
    assert store.job_counts_by_state() == {}
    assert store.delivery_stats()["pending"] == 0


def test_state_store_migrates_legacy_delivery_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import sqlite3

    db_path = tmp_path / "cron" / "cron.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '0')")
        conn.execute(
            """
            CREATE TABLE delivery_events (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                job_name TEXT,
                run_at TEXT,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                output_path TEXT,
                final_response TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO delivery_events (
                id, job_id, job_name, run_at, target, target_type, target_id,
                status, payload_json, created_at, updated_at
            ) VALUES ('event-1', 'job-1', 'daily', '2026-05-28T10:00:00+00:00',
                      'webhook:https://example.invalid/hook', 'webhook',
                      'https://example.invalid/hook', 'pending', '{}',
                      '2026-05-28T10:00:00+00:00', '2026-05-28T10:00:00+00:00')
            """
        )

    from cron.state_store import StateStore

    store = StateStore()
    event = store.get_delivery_event("event-1")

    assert store.schema_version() == 2
    assert event["adapter_key"] == "webhook"
    assert event["address"] == "https://example.invalid/hook"
    assert "run_id" in event
    assert "origin_json" in event


def test_state_store_imports_legacy_delivery_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import sqlite3

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    legacy_path = cron_dir / "delivery.sqlite3"
    with sqlite3.connect(str(legacy_path)) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                job_name TEXT,
                run_at TEXT,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                output_path TEXT,
                final_response TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO delivery_events (
                id, job_id, job_name, run_at, target, target_type, target_id,
                status, payload_json, created_at, updated_at
            ) VALUES ('legacy-event', 'job-1', 'daily', '2026-05-28T10:00:00+00:00',
                      'origin', 'origin', 'thread-1', 'pending', '{}',
                      '2026-05-28T10:00:00+00:00', '2026-05-28T10:00:00+00:00')
            """
        )

    from cron.state_store import StateStore

    store = StateStore()
    event = store.get_delivery_event("legacy-event")

    assert event["adapter_key"] == "origin"
    assert event["address"] == "thread-1"
    assert store.get_meta("legacy_delivery_sqlite_imported_at")


def test_state_store_retries_legacy_delivery_import_after_read_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import sqlite3

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    legacy_path = cron_dir / "delivery.sqlite3"
    legacy_path.write_text("not sqlite", encoding="utf-8")

    from cron.state_store import StateStore

    store = StateStore()
    assert store.get_meta("legacy_delivery_sqlite_imported_at") is None

    legacy_path.unlink()
    with sqlite3.connect(str(legacy_path)) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                job_name TEXT,
                run_at TEXT,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                output_path TEXT,
                final_response TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO delivery_events (
                id, job_id, job_name, run_at, target, target_type, target_id,
                status, payload_json, created_at, updated_at
            ) VALUES ('retry-event', 'job-1', 'daily', '2026-05-28T10:00:00+00:00',
                      'local', 'local', NULL, 'pending', '{}',
                      '2026-05-28T10:00:00+00:00', '2026-05-28T10:00:00+00:00')
            """
        )

    store = StateStore()
    assert store.get_delivery_event("retry-event")["adapter_key"] == "local"
    assert store.get_meta("legacy_delivery_sqlite_imported_at")


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
    assert jobs[0]["origin"] == {
        "source_type": "cli",
        "session_id": "thread-1",
        "thread_id": "thread-1",
    }
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


def test_state_store_migrates_legacy_unsupported_delivery_without_validating(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-job",
                        "name": "legacy",
                        "prompt": "write report",
                        "schedule": {"kind": "interval", "minutes": 60},
                        "enabled": True,
                        "state": "scheduled",
                        "next_run_at": "2026-05-28T10:00:00+00:00",
                        "repeat": {"times": None, "completed": 0},
                        "deliver": "telegram:123",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    from cron.state_store import StateStore

    job = StateStore().get_job("legacy-job")

    assert job["deliver"] == "telegram:123"
    assert job["delivery_targets"][0]["adapter_key"] == "telegram"


def test_state_store_direct_create_rejects_invalid_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import pytest

    from cron.state_store import StateStore

    store = StateStore()
    job = {
        "id": "job-1",
        "name": "bad",
        "prompt": "write report",
        "schedule": {"kind": "interval", "minutes": 60},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-05-28T10:00:00+00:00",
        "repeat": {"times": None, "completed": 0},
        "deliver": "telegram:123",
    }

    with pytest.raises(ValueError, match="unsupported delivery target: telegram:123"):
        store.create_job(job)


def test_state_store_direct_update_rejects_invalid_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import pytest

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    store = StateStore()

    with pytest.raises(ValueError, match="unsupported delivery target: telegram:123"):
        store.update_job(job["id"], {"deliver": "telegram:123"})


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


def test_jobs_facade_stores_normalized_delivery_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job

    job = create_job(
        prompt="write report",
        schedule="30m",
        name="daily",
        deliver="origin,local",
        origin={"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
    )

    assert [(target["target_type"], target["adapter_key"], target["address"]) for target in job["delivery_targets"]] == [
        ("origin", "origin", "session-1"),
        ("local", "local", None),
    ]


from datetime import datetime, timezone


def test_claim_due_job_creates_run_without_advancing_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    due_at = job["next_run_at"]

    claimed = store.claim_due_jobs(now_text=due_at, limit=10)

    assert len(claimed) == 1
    assert claimed[0]["job"]["id"] == job["id"]
    assert claimed[0]["run"]["status"] == "claimed"
    assert store.get_job(job["id"])["state"] == "running"
    assert store.get_job(job["id"])["next_run_at"] == due_at


def test_complete_run_advances_recurring_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    store = StateStore()
    claimed = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]

    completed = store.complete_run(
        claimed["run"]["id"],
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at="2099-01-01T00:30:00+00:00",
        completed=False,
    )

    assert completed["run"]["status"] == "succeeded"
    stored_job = store.get_job(job["id"])
    assert stored_job["state"] == "scheduled"
    assert stored_job["next_run_at"] == "2099-01-01T00:30:00+00:00"
    assert stored_job["lease_run_id"] is None


def test_recover_expired_leases_marks_run_abandoned(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore(lease_seconds=1)
    claimed = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]

    recovered = store.recover_expired_leases(now_text="2100-01-01T00:00:00+00:00")

    assert recovered == 1
    assert store.get_run(claimed["run"]["id"])["status"] == "abandoned"
    assert store.get_job(job["id"])["state"] == "scheduled"


def test_late_completion_does_not_clear_newer_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-28T10:00:00+00:00"})
    store = StateStore(lease_seconds=1)
    first = store.claim_due_jobs(now_text="2026-05-28T10:00:00+00:00", limit=1)[0]
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            ("2026-05-28T10:00:01+00:00", job["id"]),
        )
    store.recover_expired_leases(now_text="2026-05-28T10:01:00+00:00")
    second = store.claim_due_jobs(now_text="2026-05-28T10:01:00+00:00", limit=1)[0]

    completed = store.complete_run(
        first["run"]["id"],
        success=True,
        output_path="/tmp/late.md",
        final_response="late",
        error=None,
        next_run_at="2026-05-28T10:30:00+00:00",
        completed=False,
    )

    stored_job = store.get_job(job["id"])
    assert completed["run"]["status"] == "abandoned"
    assert completed["run"]["exit_reason"] == "late_completion"
    assert stored_job["lease_run_id"] == second["run"]["id"]
    assert stored_job["state"] == "running"


def test_claim_due_jobs_skips_stale_recurring_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    store = StateStore()

    claimed = store.claim_due_jobs(now_text="2099-01-01T00:00:00+00:00", limit=10)

    assert claimed == []
    runs = store.runs_for_job(job["id"])
    assert runs[0]["status"] == "skipped"
    assert runs[0]["exit_reason"] == "missed_run"
    assert store.get_job(job["id"])["state"] == "scheduled"
    assert store.get_job(job["id"])["next_run_at"] > "2099-01-01T00:00:00+00:00"


def test_claim_due_jobs_compares_mixed_offsets_by_instant(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-29T00:30:00+08:00"})
    store = StateStore()

    assert store.claim_due_jobs(now_text="2026-05-28T16:00:00+00:00", limit=1) == []
    assert len(store.claim_due_jobs(now_text="2026-05-28T16:30:00+00:00", limit=1)) == 1


def test_claim_due_jobs_orders_by_due_instant_before_created_at(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    store.create_job(
        {
            "id": "later-due",
            "name": "later due",
            "prompt": "later",
            "schedule": {"kind": "interval", "minutes": 60},
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-05-28T16:30:00+00:00",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
            "created_at": "2026-05-28T09:00:00+00:00",
        }
    )
    store.create_job(
        {
            "id": "earlier-due",
            "name": "earlier due",
            "prompt": "earlier",
            "schedule": {"kind": "interval", "minutes": 60},
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-05-28T16:00:00+00:00",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
            "created_at": "2026-05-28T10:00:00+00:00",
        }
    )

    claimed = store.claim_due_jobs(now_text="2026-05-28T16:30:00+00:00", limit=1)

    assert [item["job"]["id"] for item in claimed] == ["earlier-due"]


def test_claim_due_jobs_scans_past_lexically_earlier_future_offsets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    for index in range(101):
        store.create_job(
            {
                "id": f"future-{index}",
                "name": f"future {index}",
                "prompt": "future",
                "schedule": {"kind": "interval", "minutes": 60},
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2026-05-28T12:00:00-10:00",
                "repeat": {"times": None, "completed": 0},
                "deliver": "local",
                "created_at": f"2026-05-28T09:{index % 60:02d}:00+00:00",
            }
        )
    store.create_job(
        {
            "id": "due",
            "name": "due",
            "prompt": "due",
            "schedule": {"kind": "interval", "minutes": 60},
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-05-28T16:30:00+00:00",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
            "created_at": "2026-05-28T11:00:00+00:00",
        }
    )

    claimed = store.claim_due_jobs(now_text="2026-05-28T16:30:00+00:00", limit=1)

    assert [item["job"]["id"] for item in claimed] == ["due"]


def test_claim_due_jobs_enforces_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    first = create_job(prompt="first", schedule="every 30m", deliver="local")
    second = create_job(prompt="second", schedule="every 30m", deliver="local")
    update_job(first["id"], {"next_run_at": "2026-05-28T16:30:00+00:00"})
    update_job(second["id"], {"next_run_at": "2026-05-28T16:30:00+00:00"})
    store = StateStore()

    claimed = store.claim_due_jobs(now_text="2026-05-28T16:30:00+00:00", limit=1)

    assert len(claimed) == 1
    assert len([job for job in store.list_jobs(include_disabled=True) if job["state"] == "running"]) == 1


def test_claim_due_jobs_skips_malformed_timestamp_and_claims_valid_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    bad = create_job(prompt="bad", schedule="every 30m", deliver="local")
    good = create_job(prompt="good", schedule="every 30m", deliver="local")
    update_job(bad["id"], {"next_run_at": "not-a-time"})
    update_job(good["id"], {"next_run_at": "2026-05-28T16:30:00+00:00"})
    store = StateStore()

    claimed = store.claim_due_jobs(now_text="2026-05-28T16:30:00+00:00", limit=1)

    assert [item["job"]["id"] for item in claimed] == [good["id"]]


def test_recover_expired_leases_compares_mixed_offsets_by_instant(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="every 30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-28T16:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-28T16:00:00+00:00", limit=1)[0]
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            ("2026-05-28T16:30:00+00:00", job["id"]),
        )

    assert store.recover_expired_leases(now_text="2026-05-29T00:00:00+08:00") == 0
    assert store.get_run(claimed["run"]["id"])["status"] == "claimed"
    assert store.recover_expired_leases(now_text="2026-05-29T00:31:00+08:00") == 1
    assert store.get_run(claimed["run"]["id"])["status"] == "abandoned"


def test_recover_expired_leases_skips_malformed_timestamp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    bad = create_job(prompt="bad", schedule="every 30m", deliver="local")
    good = create_job(prompt="good", schedule="every 30m", deliver="local")
    update_job(bad["id"], {"next_run_at": "2026-05-28T16:00:00+00:00"})
    update_job(good["id"], {"next_run_at": "2026-05-28T16:00:00+00:00"})
    store = StateStore()
    bad_claim = store.claim_due_jobs(now_text="2026-05-28T16:00:00+00:00", limit=1)[0]
    good_claim = store.claim_due_jobs(now_text="2026-05-28T16:00:00+00:00", limit=1)[0]
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", ("not-a-time", bad["id"]))
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", ("2026-05-28T16:00:01+00:00", good["id"]))

    assert store.recover_expired_leases(now_text="2026-05-28T16:01:00+00:00") == 1
    assert store.get_run(bad_claim["run"]["id"])["status"] == "claimed"
    assert store.get_run(good_claim["run"]["id"])["status"] == "abandoned"


def test_claim_due_delivery_events_compares_mixed_offsets_by_instant(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from datetime import datetime, timezone
    import cron.state_store as state_store
    from cron.state_store import StateStore

    monkeypatch.setattr(
        state_store,
        "utc_now",
        lambda: datetime(2026, 5, 28, 16, 30, tzinfo=timezone.utc),
    )
    store = StateStore()
    event = store.enqueue_delivery_event(
        job_id="job-1",
        run_id=None,
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        adapter_key="webhook",
        address="https://example.invalid/hook",
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="failed",
    )
    store.update_delivery_event(event["id"], next_attempt_at="2026-05-29T00:30:00+08:00")

    claimed = store.claim_due_delivery_events(limit=1, adapter_keys={"webhook"})

    assert [item["id"] for item in claimed] == [event["id"]]


def test_claim_due_delivery_events_treats_malformed_next_attempt_as_due(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    event = store.enqueue_delivery_event(
        job_id="job-1",
        run_id=None,
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        adapter_key="webhook",
        address="https://example.invalid/hook",
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="failed",
    )
    store.update_delivery_event(event["id"], next_attempt_at="not-a-time")

    claimed = store.claim_due_delivery_events(limit=1, adapter_keys={"webhook"})

    assert [item["id"] for item in claimed] == [event["id"]]
