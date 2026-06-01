# Cron SQLite Delivery State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a SQLite-backed cron state machine for jobs, runs, and multi-target adapter-driven delivery.

**Architecture:** Add `StateStore` as the transactional cron persistence boundary, then keep existing public modules as compatibility facades. Delivery target parsing, adapter registration, and dispatch move into focused modules, while scheduler execution changes from pre-advancing jobs to claim/lease/complete run records.

**Tech Stack:** Python 3, stdlib `sqlite3`, `dataclasses`, existing `pytest` test suite, existing cron modules under `cron/`, CLI under `agent_cli/`, tool facade under `agent_tools/public/cronjob.py`.

---

## File Structure

- Create `cron/state_store.py`: SQLite schema, migrations, jobs JSON import, job facade operations, run claim/complete/recovery, delivery event operations.
- Create `cron/delivery_targets.py`: `DeliveryIdentity`, `DeliveryTarget`, parsing and validation helpers.
- Create `cron/delivery_adapters.py`: `DeliveryAdapter` protocol, `DeliveryResult`, built-in local/origin/webhook adapters.
- Create `cron/delivery_registry.py`: registry construction, target validation, adapter lookup.
- Create `cron/delivery_dispatcher.py`: persistent delivery event dispatcher.
- Modify `cron/jobs.py`: keep public API, delegate storage and state transitions to `StateStore`; keep schedule parsing/output file helpers local.
- Modify `cron/delivery_store.py`: compatibility wrapper over `StateStore` delivery methods.
- Modify `cron/delivery.py`: compatibility facade over target parsing, event enqueue, and dispatcher.
- Modify `cron/notifications.py`: drain persisted origin events using structured origin/session identity.
- Modify `cron/scheduler.py`: replace `get_due_jobs()`/`advance_next_run()` flow with claimed run records.
- Modify `agent_tools/public/cronjob.py`: validate delivery through registry and store structured origin identity.
- Modify `agent_cli/cron_commands.py`: status/doctor/test-delivery report sqlite, runs, adapter registry, and origin identity diagnostics.
- Modify tests:
  - `tests/test_cron_state_store.py`
  - `tests/test_cron_delivery_targets.py`
  - `tests/test_cron_delivery_adapters.py`
  - `tests/test_cron_delivery_dispatcher.py`
  - Existing `tests/test_cron_delivery.py`
  - Existing `tests/test_cron_scheduler.py`
  - Existing `tests/test_cronjob_tool.py`
  - Existing CLI cron tests if present in the current checkout.

## Task 1: Add SQLite StateStore Schema And Jobs JSON Import

**Files:**
- Create: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing schema and migration tests**

Add `tests/test_cron_state_store.py`:

```python
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
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_cron_state_store.py -q
```

Expected: fail because `cron.state_store` does not exist.

- [ ] **Step 3: Implement `StateStore` schema and import**

Create `cron/state_store.py` with these core pieces:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from cron.paths import ensure_cron_dirs, get_cron_dir, get_jobs_file, secure_file

SCHEMA_VERSION = 1
JOB_STATES = {"scheduled", "running", "paused", "completed", "error"}
RUN_STATUSES = {"claimed", "running", "succeeded", "failed", "skipped", "abandoned"}
DELIVERY_STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}
RETRY_DELAYS = (60, 300, 900, 3600, 21600)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return copy.deepcopy(default)
    return json.loads(value)


def get_state_db_path() -> Path:
    return get_cron_dir() / "cron.sqlite3"


class StateStore:
    def __init__(self, path: Path | None = None, *, max_delivery_attempts: int = 5, lease_seconds: int = 1800) -> None:
        ensure_cron_dirs()
        self.path = path or get_state_db_path()
        self.max_delivery_attempts = max(1, int(max_delivery_attempts))
        self.lease_seconds = max(1, int(lease_seconds))
        self._init_schema()
        self._import_jobs_json_if_needed()
        secure_file(self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    schedule_display TEXT,
                    enabled INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    last_delivery_error TEXT,
                    repeat_json TEXT NOT NULL,
                    deliver TEXT NOT NULL,
                    delivery_targets_json TEXT,
                    origin_json TEXT,
                    workdir TEXT,
                    script TEXT,
                    context_from_json TEXT,
                    skills_json TEXT,
                    enabled_toolsets_json TEXT,
                    model TEXT,
                    provider TEXT,
                    base_url TEXT,
                    concurrency_key TEXT,
                    lease_run_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    exit_reason TEXT,
                    output_path TEXT,
                    final_response TEXT,
                    error TEXT,
                    delivery_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_events (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    run_id TEXT,
                    job_name TEXT,
                    run_at TEXT,
                    target TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    adapter_key TEXT NOT NULL,
                    address TEXT,
                    thread_id TEXT,
                    origin_json TEXT,
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(state, enabled, next_run_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(lease_expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, scheduled_for)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, lease_expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_events(status, next_attempt_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_run ON delivery_events(job_id, run_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_target ON delivery_events(target_type, address, status, created_at)")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def schema_version(self) -> int:
        return int(self.get_meta("schema_version") or 0)

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", (key, value))
```

Add `_job_to_row_values()`, `_row_to_job()`, `create_job()`, `list_jobs()`, `get_job()`, and `_import_jobs_json_if_needed()` in the same file:

```python
    def _job_to_row_values(self, job: dict[str, Any], now_text: str | None = None) -> dict[str, Any]:
        now_text = now_text or utc_now().isoformat()
        skills = list(job.get("skills") or [])
        skill = str(job.get("skill") or "").strip()
        if skill and skill not in skills:
            skills.append(skill)
        return {
            "id": str(job["id"]),
            "name": str(job.get("name") or "cron job"),
            "prompt": str(job.get("prompt") or ""),
            "schedule_json": _json_dumps(job.get("schedule") or {}),
            "schedule_display": job.get("schedule_display"),
            "enabled": 1 if job.get("enabled", True) else 0,
            "state": str(job.get("state") or "scheduled"),
            "next_run_at": job.get("next_run_at"),
            "last_run_at": job.get("last_run_at"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
            "last_delivery_error": job.get("last_delivery_error"),
            "repeat_json": _json_dumps(job.get("repeat") or {"times": None, "completed": 0}),
            "deliver": str(job.get("deliver") or "local"),
            "delivery_targets_json": _json_dumps(job.get("delivery_targets")) if job.get("delivery_targets") is not None else None,
            "origin_json": _json_dumps(job.get("origin")) if job.get("origin") is not None else None,
            "workdir": job.get("workdir"),
            "script": job.get("script"),
            "context_from_json": _json_dumps(job.get("context_from")) if job.get("context_from") is not None else None,
            "skills_json": _json_dumps(skills),
            "enabled_toolsets_json": _json_dumps(job.get("enabled_toolsets")) if job.get("enabled_toolsets") is not None else None,
            "model": job.get("model"),
            "provider": job.get("provider"),
            "base_url": job.get("base_url"),
            "concurrency_key": job.get("concurrency_key") or job.get("workdir") or str(job["id"]),
            "lease_run_id": job.get("lease_run_id"),
            "lease_expires_at": job.get("lease_expires_at"),
            "created_at": job.get("created_at") or now_text,
            "updated_at": now_text,
        }

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        job = {
            "id": row["id"],
            "name": row["name"],
            "prompt": row["prompt"],
            "schedule": _json_loads(row["schedule_json"], {}),
            "schedule_display": row["schedule_display"],
            "enabled": bool(row["enabled"]),
            "state": row["state"],
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "last_delivery_error": row["last_delivery_error"],
            "repeat": _json_loads(row["repeat_json"], {"times": None, "completed": 0}),
            "deliver": row["deliver"],
            "origin": _json_loads(row["origin_json"], None),
            "workdir": row["workdir"],
            "script": row["script"],
            "context_from": _json_loads(row["context_from_json"], None),
            "skills": _json_loads(row["skills_json"], []),
            "enabled_toolsets": _json_loads(row["enabled_toolsets_json"], None),
            "model": row["model"],
            "provider": row["provider"],
            "base_url": row["base_url"],
            "concurrency_key": row["concurrency_key"],
            "lease_run_id": row["lease_run_id"],
            "lease_expires_at": row["lease_expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        job["skill"] = job["skills"][0] if job["skills"] else None
        delivery_targets = _json_loads(row["delivery_targets_json"], None)
        if delivery_targets is not None:
            job["delivery_targets"] = delivery_targets
        return job

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        values = self._job_to_row_values(job)
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})",
                [values[column] for column in columns],
            )
        return self.get_job(str(job["id"])) or copy.deepcopy(job)

    def list_jobs(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else self._row_to_job(row)

    def _import_jobs_json_if_needed(self) -> None:
        if self.get_meta("jobs_json_imported_at"):
            return
        jobs_file = get_jobs_file()
        if not jobs_file.exists():
            return
        with self._connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"])
        if count:
            self.set_meta("jobs_json_imported_at", utc_now().isoformat())
            return
        try:
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
            jobs = payload.get("jobs") or []
            if not isinstance(jobs, list):
                raise RuntimeError("'jobs' must be a list")
        except Exception as exc:
            raise RuntimeError(f"Failed to import cron jobs file {jobs_file}: {exc}") from exc
        now_text = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for job in jobs:
                values = self._job_to_row_values(dict(job), now_text=now_text)
                columns = list(values)
                placeholders = ", ".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})",
                    [values[column] for column in columns],
                )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('jobs_json_imported_at', ?)",
                (now_text,),
            )

    def job_counts_by_state(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}
```

Also add temporary delivery stats stub used by tests:

```python
    def delivery_stats(self) -> dict[str, int]:
        stats = {status: 0 for status in DELIVERY_STATUSES}
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_events GROUP BY status").fetchall()
        for row in rows:
            stats[str(row["status"])] = int(row["count"])
        return stats
```

- [ ] **Step 4: Run schema tests**

Run:

```bash
pytest tests/test_cron_state_store.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: add cron sqlite state store"
```

## Task 2: Move `cron.jobs` Facade To SQLite

**Files:**
- Modify: `cron/jobs.py`
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`
- Test: existing cron job facade tests in `tests/test_cronjob_tool.py`

- [ ] **Step 1: Add facade behavior tests**

Append to `tests/test_cron_state_store.py`:

```python
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
```

- [ ] **Step 2: Run failing facade test**

Run:

```bash
pytest tests/test_cron_state_store.py::test_jobs_facade_create_update_pause_resume_remove -q
```

Expected: fail while `cron.jobs` still writes `jobs.json`.

- [ ] **Step 3: Add update/delete helpers to `StateStore`**

Add to `cron/state_store.py`:

```python
    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_job(job_id)
        if existing is None:
            raise KeyError(f"Cron job not found: {job_id}")
        merged = dict(existing)
        merged.update(updates)
        values = self._job_to_row_values(merged)
        assignments = ", ".join(f"{column} = ?" for column in values if column != "id")
        params = [values[column] for column in values if column != "id"] + [job_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)
        updated = self.get_job(job_id)
        if updated is None:
            raise KeyError(f"Cron job not found after update: {job_id}")
        return updated

    def remove_job(self, job_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return bool(cursor.rowcount)
```

- [ ] **Step 4: Modify `cron.jobs` storage functions**

In `cron/jobs.py`, keep schedule parsing and normalization helpers. Change storage functions to delegate:

```python
def _store():
    from cron.state_store import StateStore

    return StateStore()


def load_jobs() -> list[dict[str, Any]]:
    return _store().list_jobs(include_disabled=True)


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    store = _store()
    existing_ids = {job["id"] for job in store.list_jobs(include_disabled=True)}
    for job in jobs:
        if job.get("id") in existing_ids:
            store.update_job(str(job["id"]), job)
        else:
            store.create_job(job)


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    return copy.deepcopy(_store().list_jobs(include_disabled=include_disabled))


def get_job(job_id: str) -> dict[str, Any] | None:
    job = _store().get_job(job_id)
    return copy.deepcopy(job) if job is not None else None
```

Change `create_job()` final block:

```python
    return copy.deepcopy(_store().create_job(job))
```

Change `update_job()`:

```python
def update_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    existing = get_job(job_id)
    if existing is None:
        raise KeyError(f"Cron job not found: {job_id}")
    normalized = _normalize_updates(updates, existing)
    return copy.deepcopy(_store().update_job(job_id, normalized))
```

Change `remove_job()`:

```python
def remove_job(job_id: str) -> bool:
    return _store().remove_job(job_id)
```

Keep `pause_job()`, `resume_job()`, and `trigger_job()` calling `update_job()` unless they need only small adaptations.

- [ ] **Step 5: Run facade tests**

Run:

```bash
pytest tests/test_cron_state_store.py tests/test_cronjob_tool.py -q
```

Expected: facade tests pass, or fail only on formatting assertions that Task 9 updates when CLI diagnostics are changed.

- [ ] **Step 6: Commit**

```bash
git add cron/jobs.py cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: store cron jobs in sqlite"
```

## Task 3: Add Delivery Target Parsing And Registry Validation

**Files:**
- Create: `cron/delivery_targets.py`
- Create: `cron/delivery_registry.py`
- Test: `tests/test_cron_delivery_targets.py`

- [ ] **Step 1: Write target parsing tests**

Create `tests/test_cron_delivery_targets.py`:

```python
from __future__ import annotations


def test_parse_multi_target_preserves_webhook_url_and_dedupes():
    from cron.delivery_targets import DeliveryIdentity, parse_delivery_targets

    origin = DeliveryIdentity(source_type="cli", session_id="session-1", thread_id="thread-1")
    targets = parse_delivery_targets(
        "origin,webhook:https://example.invalid/hook,local,origin",
        origin=origin,
    )

    assert [(target.target_type, target.adapter_key, target.address) for target in targets] == [
        ("origin", "origin", "session-1"),
        ("webhook", "webhook", "https://example.invalid/hook"),
        ("local", "local", None),
    ]


def test_origin_without_identity_fails_closed():
    from cron.delivery_targets import DeliveryTargetError, parse_delivery_targets

    try:
        parse_delivery_targets("origin", origin=None)
    except DeliveryTargetError as exc:
        assert "origin delivery requires origin identity" in str(exc)
    else:
        raise AssertionError("expected origin without identity to fail")


def test_reserved_platform_target_rejected_without_adapter():
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry()
    result = registry.validate_targets("slack:C123", origin=None, job={})

    assert result.ok is False
    assert "unsupported delivery target" in result.error
```

- [ ] **Step 2: Run failing target tests**

Run:

```bash
pytest tests/test_cron_delivery_targets.py -q
```

Expected: fail because modules do not exist.

- [ ] **Step 3: Implement target dataclasses and parser**

Create `cron/delivery_targets.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DeliveryTargetError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryIdentity:
    source_type: str
    platform: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None
    display_name: str | None = None

    @classmethod
    def from_job_origin(cls, origin: dict[str, Any] | None) -> "DeliveryIdentity | None":
        if not origin:
            return None
        return cls(
            source_type=str(origin.get("source_type") or ("gateway" if origin.get("platform") and origin.get("chat_id") else "cli")),
            platform=origin.get("platform"),
            chat_id=origin.get("chat_id"),
            thread_id=origin.get("thread_id"),
            session_id=origin.get("session_id") or origin.get("thread_id"),
            display_name=origin.get("display_name") or origin.get("chat_name"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "source_type": self.source_type,
                "platform": self.platform,
                "chat_id": self.chat_id,
                "thread_id": self.thread_id,
                "session_id": self.session_id,
                "display_name": self.display_name,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class DeliveryTarget:
    raw: str
    target_type: str
    adapter_key: str
    address: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> tuple[str, str, str | None, str | None]:
        return (self.target_type, self.adapter_key, self.address, self.thread_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "target_type": self.target_type,
            "adapter_key": self.adapter_key,
            "address": self.address,
            "thread_id": self.thread_id,
            "metadata": self.metadata,
        }


def parse_delivery_targets(deliver: str | None, *, origin: DeliveryIdentity | None) -> list[DeliveryTarget]:
    raw = str(deliver or "local").strip() or "local"
    targets: list[DeliveryTarget] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for part in [item.strip() for item in raw.split(",") if item.strip()]:
        target = _parse_one(part, origin=origin)
        key = target.dedupe_key()
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return targets


def _parse_one(raw: str, *, origin: DeliveryIdentity | None) -> DeliveryTarget:
    lowered = raw.lower()
    if lowered == "local":
        return DeliveryTarget(raw=raw, target_type="local", adapter_key="local")
    if lowered == "origin":
        if origin is None:
            raise DeliveryTargetError("origin delivery requires origin identity")
        if origin.source_type == "cli" and not (origin.session_id or origin.thread_id):
            raise DeliveryTargetError("origin delivery requires CLI session_id or thread_id")
        if origin.source_type in {"gateway", "web"} and not (origin.chat_id or origin.session_id):
            raise DeliveryTargetError("origin delivery requires chat_id or session_id")
        return DeliveryTarget(
            raw=raw,
            target_type="origin",
            adapter_key="origin",
            address=origin.session_id or origin.chat_id or origin.thread_id,
            thread_id=origin.thread_id,
            metadata={"origin": origin.to_json()},
        )
    if lowered == "webhook":
        return DeliveryTarget(raw=raw, target_type="webhook", adapter_key="webhook")
    if lowered.startswith("webhook:"):
        return DeliveryTarget(
            raw=raw,
            target_type="webhook",
            adapter_key="webhook",
            address=raw.split(":", 1)[1].strip() or None,
        )
    if ":" in raw:
        platform, rest = raw.split(":", 1)
        chat_id, sep, thread_id = rest.partition(":")
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key=platform.lower(),
            address=chat_id or None,
            thread_id=thread_id if sep else None,
        )
    return DeliveryTarget(raw=raw, target_type="platform", adapter_key=lowered)
```

- [ ] **Step 4: Implement registry validation**

Create `cron/delivery_registry.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cron.delivery_targets import DeliveryIdentity, DeliveryTarget, DeliveryTargetError, parse_delivery_targets


@dataclass(frozen=True)
class DeliveryValidation:
    ok: bool
    targets: list[DeliveryTarget]
    error: str | None = None


class DeliveryRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, adapter: Any) -> None:
        self._adapters[str(adapter.key)] = adapter

    def get(self, key: str) -> Any | None:
        return self._adapters.get(str(key))

    def adapter_keys(self) -> list[str]:
        return sorted(self._adapters)

    def validate_targets(self, deliver: str | None, *, origin: DeliveryIdentity | None, job: dict[str, Any]) -> DeliveryValidation:
        try:
            targets = parse_delivery_targets(deliver, origin=origin)
        except DeliveryTargetError as exc:
            return DeliveryValidation(False, [], str(exc))
        for target in targets:
            adapter = self.get(target.adapter_key)
            if adapter is None:
                return DeliveryValidation(False, targets, f"unsupported delivery target: {target.raw}")
            validation = adapter.validate(target, job)
            if not validation.ok:
                return DeliveryValidation(False, targets, validation.error)
        return DeliveryValidation(True, targets)


def default_delivery_registry() -> DeliveryRegistry:
    from cron.delivery_adapters import LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter())
    return registry
```

Add temporary adapter shells in `cron/delivery_adapters.py` if not created yet:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterValidation:
    ok: bool
    error: str | None = None


class LocalDeliveryAdapter:
    key = "local"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)


class OriginDeliveryAdapter:
    key = "origin"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)


class WebhookDeliveryAdapter:
    key = "webhook"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        from cron.delivery import validate_webhook_url

        error = validate_webhook_url(target.address)
        return AdapterValidation(error is None, error)
```

- [ ] **Step 5: Run target tests**

Run:

```bash
pytest tests/test_cron_delivery_targets.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add cron/delivery_targets.py cron/delivery_registry.py cron/delivery_adapters.py tests/test_cron_delivery_targets.py
git commit -m "feat: add cron delivery target registry"
```

## Task 4: Add Delivery Event Store Methods And Compatibility Wrapper

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/delivery_store.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Update delivery store tests for new event fields**

Modify `tests/test_cron_delivery.py::test_enqueue_local_result_marks_delivered` assertions to include adapter key:

```python
    assert stored["target_type"] == "local"
    assert stored["adapter_key"] == "local"
    assert stored["status"] == "delivered"
```

Add a new multi-target test:

```python
def test_enqueue_result_creates_event_per_delivery_target(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    events = enqueue_result(
        {
            "id": "job-1",
            "name": "Daily",
            "deliver": "origin,webhook:https://example.invalid/hook,local",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert isinstance(events, list)
    assert len(events) == 3
    stored = [DeliveryStore().get(event["id"]) for event in events]
    assert [(event["target_type"], event["adapter_key"], event["address"]) for event in stored] == [
        ("origin", "origin", "session-1"),
        ("webhook", "webhook", "https://example.invalid/hook"),
        ("local", "local", None),
    ]
```

- [ ] **Step 2: Run failing delivery tests**

Run:

```bash
pytest tests/test_cron_delivery.py::test_enqueue_result_creates_event_per_delivery_target -q
```

Expected: fail because `enqueue_result()` returns one event.

- [ ] **Step 3: Add delivery methods to `StateStore`**

Add methods to `cron/state_store.py`:

```python
    def enqueue_delivery_event(
        self,
        *,
        job_id: str | None,
        run_id: str | None,
        job_name: str | None,
        run_at: str | None,
        target: str,
        target_type: str,
        adapter_key: str,
        address: str | None,
        thread_id: str | None,
        origin: dict[str, Any] | None,
        final_response: str | None,
        output_path: str | None,
        payload: dict[str, Any],
        status: str = "pending",
        last_error: str | None = None,
    ) -> dict[str, Any]:
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"invalid delivery status: {status}")
        event_id = uuid.uuid4().hex
        now_text = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events (
                    id, job_id, run_id, job_name, run_at, target, target_type,
                    adapter_key, address, thread_id, origin_json, status,
                    attempt_count, next_attempt_at, last_attempt_at, last_error,
                    output_path, final_response, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    run_id,
                    job_name,
                    run_at,
                    target,
                    target_type,
                    adapter_key,
                    address,
                    thread_id,
                    _json_dumps(origin) if origin is not None else None,
                    status,
                    now_text if status in {"pending", "failed"} else None,
                    last_error,
                    output_path,
                    final_response,
                    _json_dumps(payload),
                    now_text,
                    now_text,
                ),
            )
        return self.get_delivery_event(event_id)

    def get_delivery_event(self, event_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM delivery_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return dict(row)

    def update_delivery_event(self, event_id: str, **updates: Any) -> dict[str, Any]:
        if "status" in updates and updates["status"] not in DELIVERY_STATUSES:
            raise ValueError(f"invalid delivery status: {updates['status']}")
        updates.setdefault("updated_at", utc_now().isoformat())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [event_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE delivery_events SET {assignments} WHERE id = ?", values)
        return self.get_delivery_event(event_id)

    def pending_origin_events(self, identity_ref: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE target_type = 'origin'
                  AND address = ?
                  AND status IN ('pending', 'failed')
                ORDER BY created_at
                LIMIT ?
                """,
                (str(identity_ref), max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Replace `DeliveryStore` with facade**

Replace `cron/delivery_store.py` internals with a wrapper that preserves method names:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from cron.state_store import RETRY_DELAYS, StateStore, get_state_db_path, utc_now

STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}


def get_delivery_db_path() -> Path:
    return get_state_db_path()


class DeliveryStore:
    def __init__(self, path: Path | None = None, *, max_attempts: int = 5) -> None:
        self._store = StateStore(path, max_delivery_attempts=max_attempts)
        self.path = self._store.path
        self.max_attempts = max_attempts

    def enqueue(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.enqueue_delivery_event(
            run_id=kwargs.pop("run_id", None),
            adapter_key=kwargs.pop("adapter_key", kwargs.get("target_type")),
            address=kwargs.pop("address", kwargs.pop("target_id", None)),
            thread_id=kwargs.pop("thread_id", None),
            origin=kwargs.pop("origin", None),
            **kwargs,
        )

    def get(self, event_id: str) -> dict[str, Any]:
        return self._store.get_delivery_event(event_id)

    def update_event(self, event_id: str, **updates: Any) -> dict[str, Any]:
        return self._store.update_delivery_event(event_id, **updates)

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        return self.update_event(event_id, status="delivered", last_error=None, next_attempt_at=None)

    def mark_dead(self, event_id: str, error: str) -> dict[str, Any]:
        return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)

    def stats(self) -> dict[str, int]:
        return self._store.delivery_stats()

    def pending_origin_events(self, thread_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._store.pending_origin_events(str(thread_id), limit=limit)
```

Keep `claim_due`, `mark_failed`, `recent_errors`, and `stale_delivering` by delegating after Task 6 adds those `StateStore` methods.

- [ ] **Step 5: Update `cron.delivery.enqueue_result()` for multi-target**

Modify `cron/delivery.py` so `enqueue_result()` uses `DeliveryIdentity.from_job_origin()` and `parse_delivery_targets()`. Return `None` for silent, a single dict for one target, and a list for multiple targets to preserve old callers:

```python
    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    try:
        targets = parse_delivery_targets(job.get("deliver"), origin=origin)
    except DeliveryTargetError as exc:
        targets = []
        error_text = str(exc)
    else:
        error_text = None

    events = []
    if error_text:
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=str(job.get("deliver") or "origin"),
                target_type="origin",
                adapter_key="origin",
                address=None,
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status="dead",
                last_error=error_text,
            )
        )
    for target in targets:
        status = "delivered" if target.target_type == "local" else "pending"
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                adapter_key=target.adapter_key,
                address=target.address,
                thread_id=target.thread_id,
                origin=target.metadata.get("origin"),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status=status,
            )
        )
    if not events:
        return None
    return events[0] if len(events) == 1 else events
```

- [ ] **Step 6: Run delivery tests**

Run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_targets.py -q
```

Expected: pass after adapting assertions for `address` instead of old `target_id`.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py cron/delivery_store.py cron/delivery.py tests/test_cron_delivery.py
git commit -m "feat: persist multi-target delivery events"
```

## Task 5: Implement Delivery Adapters And Dispatcher

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/delivery_adapters.py`
- Create: `cron/delivery_dispatcher.py`
- Modify: `cron/delivery.py`
- Test: `tests/test_cron_delivery_adapters.py`
- Test: `tests/test_cron_delivery_dispatcher.py`

- [ ] **Step 1: Write adapter and dispatcher tests**

Create `tests/test_cron_delivery_dispatcher.py`:

```python
from __future__ import annotations


def test_dispatcher_delivers_webhook_and_marks_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    registry = default_delivery_registry(webhook_sender=fake_post)
    summary = DeliveryDispatcher(registry=registry).dispatch_due(limit=10)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert sent[0][1]["event_id"] == event["id"]
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_dispatcher_marks_retryable_webhook_failure_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    registry = default_delivery_registry(webhook_sender=lambda url, payload, timeout=10: (500, "down"))
    summary = DeliveryDispatcher(registry=registry).dispatch_due(limit=10)

    stored = DeliveryStore().get(event["id"])
    assert summary["failed"] == 1
    assert stored["status"] == "failed"
    assert stored["attempt_count"] == 1
    assert "HTTP 500" in stored["last_error"]
    assert stored["next_attempt_at"]
```

- [ ] **Step 2: Run failing dispatcher tests**

Run:

```bash
pytest tests/test_cron_delivery_dispatcher.py -q
```

Expected: fail because dispatcher and full adapters do not exist.

- [ ] **Step 3: Add delivery claim/mark methods to `StateStore`**

Add:

```python
    def claim_due_delivery_events(self, *, limit: int = 20, adapter_keys: set[str] | None = None) -> list[dict[str, Any]]:
        now_text = utc_now().isoformat()
        params: list[Any] = [now_text]
        key_filter = ""
        if adapter_keys is not None:
            if not adapter_keys:
                return []
            placeholders = ", ".join("?" for _ in adapter_keys)
            key_filter = f" AND adapter_key IN ({placeholders})"
            params.extend(sorted(adapter_keys))
        params.append(max(0, int(limit)))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT * FROM delivery_events
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  {key_filter}
                ORDER BY created_at
                LIMIT ?
                """,
                params,
            ).fetchall()
            claimed = []
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE delivery_events
                    SET status = 'delivering',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status IN ('pending', 'failed')
                    """,
                    (now_text, now_text, row["id"]),
                )
                if cursor.rowcount:
                    claimed.append(dict(conn.execute("SELECT * FROM delivery_events WHERE id = ?", (row["id"],)).fetchone()))
        return claimed

    def mark_delivery_failed(self, event_id: str, error: str) -> dict[str, Any]:
        event = self.get_delivery_event(event_id)
        if int(event["attempt_count"]) >= self.max_delivery_attempts:
            return self.update_delivery_event(event_id, status="dead", last_error=error, next_attempt_at=None)
        index = max(0, min(int(event["attempt_count"]) - 1, len(RETRY_DELAYS) - 1))
        next_attempt = utc_now() + timedelta(seconds=RETRY_DELAYS[index])
        return self.update_delivery_event(
            event_id,
            status="failed",
            last_error=error,
            next_attempt_at=next_attempt.isoformat(),
        )
```

- [ ] **Step 4: Implement adapters**

Replace `cron/delivery_adapters.py` with full result semantics:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class AdapterValidation:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    retryable: bool = False
    error: str | None = None


class DeliveryAdapter(Protocol):
    key: str

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        ...

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        ...


class LocalDeliveryAdapter:
    key = "local"

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        output_path = event.get("output_path")
        if output_path and not Path(str(output_path)).exists():
            return DeliveryResult(False, retryable=False, error=f"local output path does not exist: {output_path}")
        return DeliveryResult(True)


class OriginDeliveryAdapter:
    key = "origin"

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        origin = json.loads(event["origin_json"]) if event.get("origin_json") else {}
        if origin.get("source_type", "cli") != "cli":
            return DeliveryResult(False, retryable=False, error="origin source is not deliverable without a live adapter")
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="origin delivery requires session_id or thread_id")
        return DeliveryResult(True)


class WebhookDeliveryAdapter:
    key = "webhook"

    def __init__(self, sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None) -> None:
        from cron.delivery import default_webhook_sender

        self.sender = sender or default_webhook_sender

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        from cron.delivery import validate_webhook_url

        error = validate_webhook_url(target.address)
        return AdapterValidation(error is None, error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="webhook delivery requires a URL")
        payload = json.loads(event["payload_json"])
        payload["event_id"] = event["id"]
        try:
            status, body = self.sender(str(event["address"]), payload, 10)
        except Exception as exc:
            return DeliveryResult(False, retryable=True, error=str(exc))
        if 200 <= status < 300:
            return DeliveryResult(True)
        if status in {408, 429} or status >= 500:
            return DeliveryResult(False, retryable=True, error=f"HTTP {status}: {body}")
        return DeliveryResult(False, retryable=False, error=f"HTTP {status}: {body}")
```

- [ ] **Step 5: Implement dispatcher**

Create `cron/delivery_dispatcher.py`:

```python
from __future__ import annotations

from typing import Any

from cron.delivery_registry import DeliveryRegistry, default_delivery_registry
from cron.state_store import StateStore


class DeliveryDispatcher:
    def __init__(self, *, store: StateStore | None = None, registry: DeliveryRegistry | None = None) -> None:
        self.store = store or StateStore()
        self.registry = registry or default_delivery_registry()

    def dispatch_due(self, *, limit: int = 20, adapter_keys: set[str] | None = None) -> dict[str, int]:
        summary = {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
        for event in self.store.claim_due_delivery_events(limit=limit, adapter_keys=adapter_keys):
            summary["claimed"] += 1
            adapter = self.registry.get(str(event["adapter_key"]))
            if adapter is None:
                self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=f"unsupported delivery target: {event['target']}",
                    next_attempt_at=None,
                )
                summary["dead"] += 1
                continue
            result = adapter.deliver(event, None, None)
            if result.delivered:
                self.store.update_delivery_event(event["id"], status="delivered", last_error=None, next_attempt_at=None)
                summary["delivered"] += 1
            elif result.retryable:
                self.store.mark_delivery_failed(event["id"], result.error or "delivery failed")
                summary["failed"] += 1
            else:
                self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=result.error or "delivery target is not deliverable",
                    next_attempt_at=None,
                )
                summary["dead"] += 1
        return summary
```

- [ ] **Step 6: Update registry factory and compatibility `process_due()`**

Change `cron/delivery_registry.py` factory:

```python
def default_delivery_registry(*, webhook_sender=None) -> DeliveryRegistry:
    from cron.delivery_adapters import LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter(sender=webhook_sender))
    return registry
```

Change `cron/delivery.py::process_due()`:

```python
def process_due(*, limit: int = 20, store=None, webhook_sender=None) -> dict[str, int]:
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry(webhook_sender=webhook_sender)
    dispatcher = DeliveryDispatcher(store=getattr(store, "_store", store), registry=registry)
    return dispatcher.dispatch_due(limit=limit)
```

- [ ] **Step 7: Run dispatcher and existing delivery tests**

Run:

```bash
pytest tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery.py -q
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add cron/state_store.py cron/delivery_adapters.py cron/delivery_registry.py cron/delivery_dispatcher.py cron/delivery.py tests/test_cron_delivery_dispatcher.py
git commit -m "feat: dispatch cron delivery through adapters"
```

## Task 6: Add Run Claim, Complete, Lease Recovery, And Missed Run Skip

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/jobs.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write run state tests**

Append to `tests/test_cron_state_store.py`:

```python
from datetime import datetime, timezone


def test_claim_due_job_creates_run_without_advancing_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    due_at = job["next_run_at"]

    claimed = store.claim_due_jobs(now_text="2099-01-01T00:00:00+00:00", limit=10)

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
    claimed = store.claim_due_jobs(now_text="2099-01-01T00:00:00+00:00", limit=1)[0]

    completed = store.complete_run(
        claimed["run"]["id"],
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at="2099-01-01T00:30:00+00:00",
        completed=True,
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
    claimed = store.claim_due_jobs(now_text="2099-01-01T00:00:00+00:00", limit=1)[0]

    recovered = store.recover_expired_leases(now_text="2100-01-01T00:00:00+00:00")

    assert recovered == 1
    assert store.get_run(claimed["run"]["id"])["status"] == "abandoned"
    assert store.get_job(job["id"])["state"] == "scheduled"
```

- [ ] **Step 2: Run failing run tests**

Run:

```bash
pytest tests/test_cron_state_store.py::test_claim_due_job_creates_run_without_advancing_next_run tests/test_cron_state_store.py::test_complete_run_advances_recurring_job tests/test_cron_state_store.py::test_recover_expired_leases_marks_run_abandoned -q
```

Expected: fail because run methods do not exist.

- [ ] **Step 3: Implement run helpers in `StateStore`**

Add methods:

```python
    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_run(row)

    def claim_due_jobs(self, *, now_text: str, limit: int = 20) -> list[dict[str, Any]]:
        lease_expires = (datetime.fromisoformat(now_text.replace("Z", "+00:00")) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE enabled = 1
                  AND state = 'scheduled'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                ORDER BY next_run_at, created_at
                LIMIT ?
                """,
                (now_text, max(0, int(limit))),
            ).fetchall()
            claimed = []
            for row in rows:
                job = self._row_to_job(row)
                run_id = uuid.uuid4().hex
                now_actual = utc_now().isoformat()
                previous_attempts = conn.execute(
                    "SELECT COUNT(*) AS count FROM runs WHERE job_id = ?",
                    (job["id"],),
                ).fetchone()["count"]
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, job_id, scheduled_for, claimed_at, lease_expires_at,
                        started_at, finished_at, attempt, status, exit_reason,
                        output_path, final_response, error, delivery_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'claimed', NULL, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (run_id, job["id"], job["next_run_at"], now_actual, lease_expires, int(previous_attempts) + 1, now_actual, now_actual),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'running',
                        lease_run_id = ?,
                        lease_expires_at = ?,
                        updated_at = ?
                    WHERE id = ? AND state = 'scheduled'
                    """,
                    (run_id, lease_expires, now_actual, job["id"]),
                )
                run = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
                claimed.append({"job": self.get_job(job["id"]) or job, "run": run})
        return claimed

    def mark_run_started(self, run_id: str) -> dict[str, Any]:
        now_text = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
                (now_text, now_text, run_id),
            )
        return self.get_run(run_id)

    def complete_run(
        self,
        run_id: str,
        *,
        success: bool,
        output_path: str | None,
        final_response: str | None,
        error: str | None,
        next_run_at: str | None,
        completed: bool,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        now_text = utc_now().isoformat()
        run_status = "succeeded" if success else "failed"
        job_state = "completed" if completed else "scheduled"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, output_path = ?, final_response = ?,
                    error = ?, updated_at = ?
                WHERE id = ?
                """,
                (run_status, now_text, output_path, final_response, error, now_text, run_id),
            )
            conn.execute(
                """
                UPDATE jobs
                SET state = ?, enabled = ?, next_run_at = ?, lease_run_id = NULL,
                    lease_expires_at = NULL, last_run_at = ?, last_status = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (job_state, 0 if completed else 1, next_run_at, now_text, "ok" if success else "error", None if success else error, now_text, run["job_id"]),
            )
        return {"run": self.get_run(run_id), "job": self.get_job(run["job_id"])}

    def recover_expired_leases(self, *, now_text: str) -> int:
        now_actual = utc_now().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.id AS run_id, runs.job_id AS job_id
                FROM runs
                JOIN jobs ON jobs.lease_run_id = runs.id
                WHERE jobs.state = 'running'
                  AND jobs.lease_expires_at IS NOT NULL
                  AND jobs.lease_expires_at <= ?
                  AND runs.status IN ('claimed', 'running')
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status = 'abandoned', finished_at = ?, exit_reason = 'lease_expired', updated_at = ? WHERE id = ?",
                    (now_actual, now_actual, row["run_id"]),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'scheduled', lease_run_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (now_actual, row["job_id"]),
                )
        return len(rows)
```

- [ ] **Step 4: Run run state tests**

Run:

```bash
pytest tests/test_cron_state_store.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: add cron run lease state machine"
```

## Task 7: Wire Scheduler To Claimed Runs

**Files:**
- Modify: `cron/scheduler.py`
- Modify: `cron/jobs.py`
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Replace scheduler tests for no pre-advance behavior**

In `tests/test_cron_scheduler.py`, replace `test_tick_advances_before_run_and_marks_result` with:

```python
def test_tick_claims_run_and_completes_without_pre_advance(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
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

    result = scheduler.tick(now_dt=RUN_AT.replace(year=2099), job_runner=fake_runner)

    runs = store.runs_for_job(job["id"])
    assert result.due == 1
    assert result.ran == 1
    assert runs[0]["status"] == "succeeded"
    assert calls[0][0] == "run"
    assert store.get_job(job["id"])["state"] in {"scheduled", "completed"}
```

- [ ] **Step 2: Run failing scheduler test**

Run:

```bash
pytest tests/test_cron_scheduler.py::test_tick_claims_run_and_completes_without_pre_advance -q
```

Expected: fail because scheduler still uses `advance_next_run()`.

- [ ] **Step 3: Add `runs_for_job()` and next-run completion helper**

Add to `StateStore`:

```python
    def runs_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE job_id = ? ORDER BY created_at", (job_id,)).fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Modify scheduler processing**

In `cron/scheduler.py`, remove direct dependency on `advance_next_run` and `get_due_jobs` for the normal path. Add:

```python
def _store():
    from cron.state_store import StateStore

    return StateStore()
```

Change `_process_job()` to accept a claimed item:

```python
def _process_claimed(
    claimed: dict[str, Any],
    run_at: datetime,
    job_runner: JobRunner,
) -> JobTickResult:
    store = _store()
    job = dict(claimed["job"])
    run = dict(claimed["run"])
    job_id = str(job["id"])
    job["run_id"] = run["id"]
    try:
        store.mark_run_started(run["id"])
        result = job_runner(job)
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        from cron.delivery import enqueue_result, process_due

        delivery_events = enqueue_result(job, result, output_path, run_at)
        process_due(limit=20)
        next_run_at, completed = _next_run_after_completion(job, run_at)
        store.complete_run(
            run["id"],
            success=result.success,
            output_path=output_path,
            final_response=result.final_response,
            error=result.error,
            next_run_at=next_run_at,
            completed=completed,
        )
        mark_job_run(job_id, success=result.success, error=result.error, run_at=run_at, delivery_error=None)
        return JobTickResult(job_id=job_id, success=result.success, output_path=output_path, error=result.error)
    except Exception as exc:
        logger.exception("Cron job %s failed during tick.", job_id)
        store.complete_run(
            run["id"],
            success=False,
            output_path=None,
            final_response=None,
            error=str(exc),
            next_run_at=job.get("next_run_at"),
            completed=False,
        )
        return JobTickResult(job_id=job_id, success=False, error=str(exc))
```

Add `_next_run_after_completion()` using existing `compute_next_run()` and repeat logic from `mark_job_run()`:

```python
def _next_run_after_completion(job: dict[str, Any], run_at: datetime) -> tuple[str | None, bool]:
    from cron.jobs import compute_next_run

    repeat = dict(job.get("repeat") or {"times": None, "completed": 0})
    repeat["completed"] = int(repeat.get("completed") or 0) + 1
    repeat_times = repeat.get("times")
    completed = repeat_times is not None and repeat["completed"] >= int(repeat_times)
    if completed:
        return None, True
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind == "once":
        return None, True
    if kind == "interval":
        return compute_next_run(schedule, base=run_at), False
    if kind == "cron":
        return compute_next_run(schedule, base=run_at), False
    return None, False
```

Change `tick()`:

```python
        store = _store()
        store.recover_expired_leases(now_text=run_at.isoformat())
        claimed = store.claim_due_jobs(now_text=run_at.isoformat(), limit=100)
        result = TickResult(due=len(claimed))
        if not claimed:
            return result
        resolved_runner: JobRunner = job_runner if job_runner is not None else _run_default_job
        workdir_claimed = [item for item in claimed if item["job"].get("workdir")]
        parallel_claimed = [item for item in claimed if not item["job"].get("workdir")]
        for item in workdir_claimed:
            result.results.append(_process_claimed(item, run_at, resolved_runner))
        result.results.extend(_run_parallel_claimed(parallel_claimed, run_at, resolved_runner))
```

Add `_run_parallel_claimed()` mirroring `_run_parallel()`.

- [ ] **Step 5: Run scheduler tests**

Run:

```bash
pytest tests/test_cron_scheduler.py tests/test_cron_state_store.py -q
```

Expected: pass after updating old expectations that asserted `advance_next_run()` was called.

- [ ] **Step 6: Commit**

```bash
git add cron/scheduler.py cron/state_store.py tests/test_cron_scheduler.py
git commit -m "feat: run cron jobs through sqlite leases"
```

## Task 8: Capture Structured Origin Identity In CLI And Tool

**Files:**
- Modify: `agent_tools/public/cronjob.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `cron/jobs.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Update cronjob tool origin tests**

Update `tests/test_cronjob_tool.py::test_cronjob_create_captures_runtime_thread` expected origin:

```python
    assert created["origin"] == {
        "source_type": "cli",
        "session_id": "thread-1",
        "thread_id": "thread-1",
    }
```

Add:

```python
def test_cronjob_explicit_origin_without_thread_fails_closed(monkeypatch):
    cronjob_tool = _cronjob_tool()

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        deliver="origin",
        runtime=None,
    )

    assert result["success"] is False
    assert result["code"] == "missing_origin_thread"
```

- [ ] **Step 2: Run failing origin tests**

Run:

```bash
pytest tests/test_cronjob_tool.py::test_cronjob_create_captures_runtime_thread tests/test_cronjob_tool.py::test_cronjob_explicit_origin_without_thread_fails_closed -q
```

Expected: first fails on old origin shape.

- [ ] **Step 3: Add origin builder**

In `agent_tools/public/cronjob.py`, add:

```python
def _origin_identity_from_thread(thread_id: str | None) -> dict[str, str] | None:
    if not thread_id:
        return None
    return {
        "source_type": "cli",
        "session_id": str(thread_id),
        "thread_id": str(thread_id),
    }
```

Change create branch:

```python
            thread_id = origin_thread_id or _runtime_thread_id(runtime)
            if deliver == "origin" and not thread_id:
                return {
                    "success": False,
                    "code": "missing_origin_thread",
                    "error": "deliver='origin' requires an active thread id.",
                }
            origin = _origin_identity_from_thread(thread_id) if thread_id and deliver in {None, "origin"} else None
```

In `agent_cli/cron_commands.py`, when setting origin for CLI session, use same shape:

```python
    origin = (
        {"source_type": "cli", "session_id": session_id, "thread_id": session_id}
        if deliver == "origin"
        else None
    )
```

Pass `origin=origin` to `run_cronjob_action()`.

- [ ] **Step 4: Run cronjob tests**

Run:

```bash
pytest tests/test_cronjob_tool.py -q
```

Expected: pass after updating expected origin shape.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/cronjob.py agent_cli/cron_commands.py tests/test_cronjob_tool.py
git commit -m "feat: store structured cron origin identity"
```

## Task 9: Update CLI Status, Doctor, And Test Delivery

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: existing CLI cron command tests

- [ ] **Step 1: Add CLI diagnostics tests**

If no focused file exists, create `tests/test_cron_commands.py`:

```python
from __future__ import annotations


def test_cron_status_includes_sqlite_and_delivery_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_status

    result = cron_status()

    assert result.exit_code == 0
    assert "Cron sqlite:" in result.text
    assert "Delivery adapters:" in result.text
    assert "Delivery queue:" in result.text


def test_cron_doctor_reports_scheduler_and_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "cron sqlite:" in result.text
    assert "delivery adapters:" in result.text
```

- [ ] **Step 2: Run failing CLI diagnostics tests**

Run:

```bash
pytest tests/test_cron_commands.py -q
```

Expected: fail until status/doctor include new lines.

- [ ] **Step 3: Update status output**

In `agent_cli/cron_commands.py::cron_status()`, add:

```python
    from cron.state_store import StateStore
    from cron.delivery_registry import default_delivery_registry

    store = StateStore()
    registry = default_delivery_registry()
    lines.extend(
        [
            f"Cron sqlite: {store.path}",
            "Job states: "
            + " ".join(f"{key}={value}" for key, value in sorted(store.job_counts_by_state().items())),
            f"Delivery adapters: {', '.join(registry.adapter_keys())}",
        ]
    )
```

Keep existing output lines so users do not lose diagnostics.

- [ ] **Step 4: Update doctor checks**

In `cron_doctor()`, add checks:

```python
    try:
        from cron.state_store import StateStore
        from cron.delivery_registry import default_delivery_registry

        store = StateStore()
        add("ok", f"cron sqlite: {store.path}")
        add("ok", f"delivery adapters: {', '.join(default_delivery_registry().adapter_keys())}")
        if get_jobs_file().exists() and not store.get_meta("jobs_json_imported_at"):
            add("warn", "jobs.json exists but import marker is missing")
    except Exception as exc:
        add("fail", f"cron sqlite error: {exc}")
```

- [ ] **Step 5: Run CLI diagnostics tests**

Run:

```bash
pytest tests/test_cron_commands.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_cron_commands.py
git commit -m "feat: report cron sqlite delivery health"
```

## Task 10: Final Compatibility Sweep And Full Test Run

**Files:**
- Modify: `cron/jobs.py` if returned job dictionaries are missing legacy keys.
- Modify: `cron/scheduler.py` if tick result counts or `JobTickResult` fields regress.
- Modify: `cron/delivery.py` if compatibility `enqueue_result()` or `process_due()` signatures regress.
- Modify: `cron/delivery_store.py` if `DeliveryStore` wrapper methods are missing.
- Modify: `cron/notifications.py` if origin drain behavior regresses.
- Modify: `agent_cli/cron_commands.py` if status, doctor, or test-delivery output regresses.
- Modify: `agent_tools/public/cronjob.py` if cronjob tool result codes or origin capture regress.
- Modify: affected tests only when assertions encode old storage internals rather than public behavior.

- [ ] **Step 1: Run focused cron suite**

Run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_targets.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py tests/test_cron_scheduler.py tests/test_cron_notifications.py tests/test_cronjob_tool.py tests/test_cron_commands.py -q
```

Expected: pass.

- [ ] **Step 2: Fix compatibility failures by preserving public shapes**

For failures about returned job fields, update `StateStore._row_to_job()` to include missing legacy keys rather than changing callers. The returned job must include:

```python
job.setdefault("skill", job["skills"][0] if job["skills"] else None)
job.setdefault("origin", _json_loads(row["origin_json"], None))
job.setdefault("repeat", _json_loads(row["repeat_json"], {"times": None, "completed": 0}))
```

For failures about `DeliveryStore` missing methods, add delegating wrappers rather than making tests import `StateStore` directly.

- [ ] **Step 3: Run broader relevant test files**

Run:

```bash
pytest tests/test_agent_cli_main.py tests/test_agent_cli_checkpoints.py tests/test_session_context.py -q
```

Expected: pass or only unrelated existing failures. Investigate any cron/CLI regressions.

- [ ] **Step 4: Run import smoke checks**

Run:

```bash
python - <<'PY'
from cron.state_store import StateStore
from cron.delivery_registry import default_delivery_registry
from cron.scheduler import tick
from agent_tools.public.cronjob import run_cronjob_action
print(StateStore().schema_version())
print(default_delivery_registry().adapter_keys())
print(callable(tick), callable(run_cronjob_action))
PY
```

Expected output includes:

```text
1
['local', 'origin', 'webhook']
True True
```

- [ ] **Step 5: Inspect git diff**

Run:

```bash
git diff --stat
git diff -- cron state_store.py
```

Expected: only intentional cron delivery/state changes and related tests.

- [ ] **Step 6: Commit final fixes**

```bash
git add cron agent_cli agent_tools tests
git commit -m "test: stabilize cron sqlite delivery migration"
```

## Self-Review

- Spec coverage:
  - SQLite store: Tasks 1, 2, 6.
  - jobs JSON migration: Task 1.
  - Delivery identity: Tasks 3, 8.
  - Delivery targets and multi-target: Tasks 3, 4.
  - Adapter registry and dispatcher: Task 5.
  - Scheduler claim/lease/complete: Tasks 6, 7.
  - CLI diagnostics: Task 9.
  - Compatibility facades: Tasks 2, 4, 7, 10.
- Placeholder scan: no `TBD`, no incomplete task placeholders, no unspecified test steps.
- Type consistency:
  - `DeliveryIdentity`, `DeliveryTarget`, `DeliveryRegistry`, `DeliveryDispatcher`, and `StateStore` names are consistent across tasks.
  - Delivery event fields use `adapter_key`, `address`, `thread_id`, and `origin_json` throughout.
  - Run claim objects consistently use `{"job": job, "run": run}`.
