from __future__ import annotations

from datetime import datetime, timezone
import json
import threading


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

    # Note: After recovery marks the run abandoned, complete_run() is now idempotent
    # and returns the existing state (lease_expired). The test verifies that the
    # newer lease is preserved, not the exit_reason of the old run.
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
    # Exit reason reflects idempotent behavior: recovery set 'lease_expired', and
    # complete_run returns early without overwriting it.
    assert completed["run"]["exit_reason"] == "lease_expired"
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


def test_recover_expired_run_lease_preserves_scheduled_occurrence(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore(lease_seconds=1)
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)[0]
    run_id = claimed["run"]["id"]

    recovered = store.recover_expired_leases(now_text="2026-05-30T10:10:00+00:00")
    recovered_job = store.get_job(job["id"])
    recovered_run = store.get_run(run_id)

    assert recovered == 1
    assert recovered_run["status"] == "abandoned"
    assert recovered_run["exit_reason"] == "lease_expired"
    assert recovered_job["state"] == "scheduled"
    assert recovered_job["lease_run_id"] is None
    assert recovered_job["lease_expires_at"] is None
    assert recovered_job["next_run_at"] == due_at


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


def test_claim_due_jobs_creates_run_without_advancing_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore()
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)

    assert len(claimed) == 1
    claimed_job = claimed[0]["job"]
    run = claimed[0]["run"]
    assert run["job_id"] == job["id"]
    assert run["scheduled_for"] == due_at
    assert run["status"] == "claimed"
    assert claimed_job["state"] == "running"
    assert claimed_job["lease_run_id"] == run["id"]
    assert claimed_job["next_run_at"] == due_at
    assert store.get_job(job["id"])["next_run_at"] == due_at


def test_second_claim_does_not_duplicate_running_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore()
    first = store.claim_due_jobs(now_text=due_at, limit=1)
    second = store.claim_due_jobs(now_text=due_at, limit=1)

    assert len(first) == 1
    assert second == []
    assert len(store.runs_for_job(job["id"])) == 1


def test_complete_run_cannot_advance_or_count_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    import cron.state_store as state_store
    from cron.state_store import StateStore

    due_dt = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(state_store, "utc_now", lambda: due_dt)
    job = create_job(
        prompt="write report",
        schedule="30m",
        deliver="local",
        repeat=2,
    )
    due_at = due_dt.isoformat()
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore(lease_seconds=86400)
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)[0]
    run_id = claimed["run"]["id"]
    assert store.mark_run_started(run_id) is not None

    first = store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at="2026-05-30T10:30:00+00:00",
        completed=False,
    )
    second = store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done again",
        error=None,
        next_run_at="2026-05-30T11:00:00+00:00",
        completed=True,
    )

    first_job = first["job"]
    second_job = second["job"]
    assert first_job["repeat"]["completed"] == 1
    assert second_job["repeat"]["completed"] == 1
    assert second_job["next_run_at"] == "2026-05-30T10:30:00+00:00"
    assert second_job["state"] == "scheduled"
    assert second["run"]["status"] == "succeeded"


def test_complete_run_uses_time_after_waiting_for_write_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    import cron.state_store as state_store
    from cron.state_store import StateStore

    due_dt = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
    expired_dt = datetime(2026, 5, 30, 10, 0, 2, tzinfo=timezone.utc)
    clock_values = [due_dt]
    monkeypatch.setattr(state_store, "utc_now", lambda: clock_values[0])

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = due_dt.isoformat()
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore(lease_seconds=1)
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)[0]
    run_id = claimed["run"]["id"]
    assert store.mark_run_started(run_id) is not None

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    result_holder: dict[str, dict] = {}
    error_holder: list[BaseException] = []

    def hold_write_lock() -> None:
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lock_acquired.set()
            release_lock.wait(timeout=5)

    def complete_after_blocking() -> None:
        try:
            result_holder["result"] = store.complete_run(
                run_id,
                success=True,
                output_path="/tmp/out.md",
                final_response="done",
                error=None,
                next_run_at="2026-05-30T10:30:00+00:00",
                completed=False,
            )
        except BaseException as exc:
            error_holder.append(exc)

    locker = threading.Thread(target=hold_write_lock)
    locker.start()
    assert lock_acquired.wait(timeout=5)
    worker = threading.Thread(target=complete_after_blocking)
    worker.start()
    clock_values[0] = expired_dt
    release_lock.set()
    locker.join(timeout=5)
    worker.join(timeout=5)

    assert not locker.is_alive()
    assert not worker.is_alive()
    assert error_holder == []
    result = result_holder["result"]
    stored_job = store.get_job(job["id"])
    assert result["run"]["status"] == "abandoned"
    assert result["run"]["exit_reason"] == "late_completion"
    assert stored_job["state"] == "scheduled"
    assert stored_job["next_run_at"] == due_at
    assert stored_job["repeat"]["completed"] == 0


def test_resolve_job_timeouts_uses_job_then_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_TIMEOUT", "77")

    from cron.state_store import StateStore

    store = StateStore()
    assert store.resolve_job_timeouts({}) == {
        "idle_timeout_seconds": 77,
        "max_runtime_seconds": None,
    }
    assert store.resolve_job_timeouts({"idle_timeout_seconds": 12, "max_runtime_seconds": 30}) == {
        "idle_timeout_seconds": 12,
        "max_runtime_seconds": 30,
    }


def test_status_query_helpers_return_next_running_and_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    assert store.list_next_due_jobs(limit=1)[0]["id"] == job["id"]

    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    assert store.list_running_runs(limit=1)[0]["run_id"] == run_id

    store.complete_run(
        run_id,
        success=False,
        output_path=None,
        final_response=None,
        error="boom",
        next_run_at=None,
        completed=True,
    )
    assert store.latest_failed_run()["run_id"] == run_id


def test_imported_jobs_json_job_runs_through_state_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    import cron.scheduler as scheduler
    import cron.state_store as state_store
    from cron.state_store import StateStore

    due_dt = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
    due_at = due_dt.isoformat()
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-job",
                        "name": "Legacy Job",
                        "prompt": "write report",
                        "schedule": {"kind": "interval", "minutes": 30},
                        "schedule_display": "30m",
                        "enabled": True,
                        "state": "scheduled",
                        "next_run_at": due_at,
                        "repeat": {"times": None, "completed": 0},
                        "deliver": "local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_store, "utc_now", lambda: due_dt)
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: str(tmp_path / "out.md"))

    result = scheduler.tick(
        now_dt=due_dt,
        job_runner=lambda claimed_job: JobRunResult(True, "# out", "done", None),
    )
    store = StateStore()
    runs = store.runs_for_job("legacy-job")
    imported_job = store.get_job("legacy-job")

    assert result.ran == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["scheduled_for"] == due_at
    assert imported_job["state"] == "scheduled"
    assert imported_job["lease_run_id"] is None
    assert imported_job["next_run_at"] != due_at


def test_run_activity_columns_and_claim_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()

    claimed = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]
    run = store.get_run(claimed["run"]["id"])

    assert run["heartbeat_at"] is not None
    assert run["last_activity_at"] is not None
    assert run["last_activity_desc"] == "claimed"
    assert run["current_tool"] is None


def test_update_run_activity_can_heartbeat_without_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    started = store.mark_run_started(run_id)
    original_activity = started["last_activity_at"]

    updated = store.update_run_activity(run_id, heartbeat=True, activity=False)

    assert updated["heartbeat_at"] is not None
    assert updated["last_activity_at"] == original_activity
    assert updated["last_activity_desc"] == "started"


def test_terminal_run_ignores_late_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at=None,
        completed=True,
    )

    assert store.update_run_activity(run_id, activity=True, last_activity_desc="agent_running") is None
    assert store.get_run(run_id)["last_activity_desc"] == "completed"


def test_resolve_job_timeouts_uses_job_then_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_TIMEOUT", "77")

    from cron.state_store import StateStore

    store = StateStore()
    assert store.resolve_job_timeouts({}) == {
        "idle_timeout_seconds": 77,
        "max_runtime_seconds": None,
    }
    assert store.resolve_job_timeouts({"idle_timeout_seconds": 12, "max_runtime_seconds": 30}) == {
        "idle_timeout_seconds": 12,
        "max_runtime_seconds": 30,
    }


def test_status_query_helpers_return_next_running_and_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    assert store.list_next_due_jobs(limit=1)[0]["id"] == job["id"]

    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    assert store.list_running_runs(limit=1)[0]["run_id"] == run_id

    store.complete_run(
        run_id,
        success=False,
        output_path=None,
        final_response=None,
        error="boom",
        next_run_at=None,
        completed=True,
    )
    assert store.latest_failed_run()["run_id"] == run_id


def _due_job(store, *, job_id, key="key:one", policy="queue_one", next_run_at="2026-05-29T10:00:00+00:00"):
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt=f"job {job_id}",
        schedule="every 5m",
        concurrency_key=key,
        concurrency_policy=policy,
    )
    return update_job(
        job["id"],
        {
            "next_run_at": next_run_at,
            "state": "scheduled",
            "enabled": True,
        },
    )


def test_claim_due_jobs_queue_one_creates_single_queued_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    first = _due_job(store, job_id="first", key="repo:a", policy="queue_one")
    second = _due_job(store, job_id="second", key="repo:a", policy="queue_one")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["run"]["status"] == "claimed"

    claimed_again = store.claim_due_jobs(now_text="2026-05-29T10:05:00+00:00", limit=10)
    assert claimed_again == []

    runs = store.list_runs(limit=20)
    queued = [run for run in runs if run["status"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["concurrency_key"] == "repo:a"
    assert queued[0]["concurrency_policy"] == "queue_one"


def test_claim_due_jobs_queue_all_records_each_due_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="queue_all")
    _due_job(store, job_id="second", key="repo:a", policy="queue_all")

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    store.claim_due_jobs(now_text="2026-05-29T10:05:00+00:00", limit=10)

    queued = [run for run in store.list_runs(limit=20) if run["status"] == "queued"]
    assert len(queued) == 2


def test_claim_due_jobs_skip_if_running_records_skipped_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="skip_if_running")
    _due_job(store, job_id="second", key="repo:a", policy="skip_if_running")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    assert len(claimed) == 1

    skipped = [run for run in store.list_runs(limit=20) if run["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["exit_reason"] == "concurrency_skip"


def test_claim_due_jobs_queue_one_idempotent_when_already_queued(monkeypatch, tmp_path):
    """Third+ claim should not create additional queued runs."""
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="queue_one")
    _due_job(store, job_id="second", key="repo:a", policy="queue_one")

    # First claim: claimed + queued
    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    # Second claim: already occupied, skip (no new queued)
    store.claim_due_jobs(now_text="2026-05-29T10:05:00+00:00", limit=10)

    # Third claim: still occupied and already queued, should not create another queued
    store.claim_due_jobs(now_text="2026-05-29T10:10:00+00:00", limit=10)

    queued = [run for run in store.list_runs(limit=20) if run["status"] == "queued"]
    assert len(queued) == 1, "Should only have one queued run"


def test_plan_job_claim_reports_queue_decision_without_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    job = _due_job(store, job_id="first", key="repo:a", policy="queue_one")
    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    plan = store.plan_job_claim(
        str(job["id"]),
        now_text="2026-05-29T10:05:00+00:00",
    )

    assert plan["decision"] == "would_queue"
    assert plan["concurrency_key"] == "repo:a"
    assert len(store.list_runs(job_id=job["id"])) == 1


def test_claim_due_jobs_replace_running_abandons_active_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="replace_running")
    _due_job(store, job_id="second", key="repo:a", policy="replace_running")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    runs = store.list_runs(limit=20)
    abandoned = [run for run in runs if run["status"] == "abandoned"]
    active = [run for run in runs if run["status"] == "claimed"]

    assert len(claimed) == 2
    assert len(abandoned) == 1
    assert abandoned[0]["exit_reason"] == "replaced_by_newer_run"
    assert abandoned[0]["replaced_by_run_id"] == active[-1]["id"]


def test_promote_queued_runs_claims_one_per_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import cron.state_store as state_store_module
    from cron.state_store import StateStore

    due_dt = datetime(2026, 5, 29, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(state_store_module, "utc_now", lambda: due_dt)

    store = StateStore()
    # Create three jobs: two for repo:a (will queue), one for repo:b
    # Due order: a1 first (claimed), a2 second (queued), b1 third (claimed)
    for i, (job_id, key) in enumerate([
        ("a1", "repo:a"),
        ("a2", "repo:a"),
        ("b1", "repo:b"),
    ]):
        store.create_job({
            "id": job_id,
            "name": f"job {job_id}",
            "prompt": f"job {job_id}",
            "schedule": {"kind": "interval", "minutes": 5},
            "schedule_display": "every 5m",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-05-29T10:00:00+00:00",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
            "concurrency_key": key,
            "concurrency_policy": "queue_all",
            "created_at": f"2026-05-29T10:00:00+00:0{i}",
        })

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    # Complete the repo:a claimed run (a1's run) to free up the key
    a_claimed = next(r for r in store.list_runs(limit=20)
                      if r["status"] == "claimed" and r.get("concurrency_key") == "repo:a")
    store.complete_run(
        a_claimed["id"],
        success=True,
        output_path=None,
        final_response="ok",
        error=None,
        next_run_at=None,
        completed=False,
    )

    promoted = store.promote_queued_runs(
        now_text="2026-05-29T10:01:00+00:00",
        limit=10,
    )

    # Only repo:a should be promoted (repo:a had a queued run and now the key is free)
    # repo:b should NOT be promoted (it already had a claimed run, no queued run for it)
    promoted_keys = [item["run"]["concurrency_key"] for item in promoted]
    assert promoted_keys == ["repo:a"]


def test_promote_queued_runs_skips_key_with_running_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="a1", key="repo:a", policy="queue_all")
    _due_job(store, job_id="a2", key="repo:a", policy="queue_all")

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    promoted = store.promote_queued_runs(
        now_text="2026-05-29T10:01:00+00:00",
        limit=10,
    )

    assert promoted == []
