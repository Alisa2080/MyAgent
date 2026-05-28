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
                    paused_reason TEXT,
                    paused_at TEXT,
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
            "paused_reason": job.get("paused_reason"),
            "paused_at": job.get("paused_at"),
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
            "paused_reason": row["paused_reason"],
            "paused_at": row["paused_at"],
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

    def delivery_stats(self) -> dict[str, int]:
        stats = {status: 0 for status in DELIVERY_STATUSES}
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_events GROUP BY status").fetchall()
        for row in rows:
            stats[str(row["status"])] = int(row["count"])
        return stats

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
