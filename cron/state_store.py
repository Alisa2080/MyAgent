from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from cron.paths import ensure_cron_dirs, get_cron_dir, get_jobs_file, secure_file
from cron.delivery_targets import DeliveryIdentity, DeliveryTargetError, parse_delivery_targets

SCHEMA_VERSION = 2
JOB_STATES = {"scheduled", "running", "paused", "completed", "error"}
RUN_STATUSES = {"claimed", "running", "succeeded", "failed", "skipped", "abandoned"}
DELIVERY_STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}
RETRY_DELAYS = (60, 300, 900, 3600, 21600)
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


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
        self._import_legacy_delivery_db_if_needed()
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_leases (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    pid INTEGER,
                    hostname TEXT,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            self._init_poll_state_schema()
            self._migrate_schema(conn)
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

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "jobs",
            {
                "prompt": "TEXT NOT NULL DEFAULT ''",
                "schedule_json": "TEXT NOT NULL DEFAULT '{}'",
                "schedule_display": "TEXT",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "state": "TEXT NOT NULL DEFAULT 'scheduled'",
                "next_run_at": "TEXT",
                "last_run_at": "TEXT",
                "last_status": "TEXT",
                "last_error": "TEXT",
                "last_delivery_error": "TEXT",
                "repeat_json": "TEXT NOT NULL DEFAULT '{\"times\": null, \"completed\": 0}'",
                "deliver": "TEXT NOT NULL DEFAULT 'local'",
                "delivery_targets_json": "TEXT",
                "origin_json": "TEXT",
                "workdir": "TEXT",
                "script": "TEXT",
                "context_from_json": "TEXT",
                "skills_json": "TEXT",
                "enabled_toolsets_json": "TEXT",
                "model": "TEXT",
                "provider": "TEXT",
                "base_url": "TEXT",
                "concurrency_key": "TEXT",
                "concurrency_policy": "TEXT",
                "lease_run_id": "TEXT",
                "lease_expires_at": "TEXT",
                "paused_reason": "TEXT",
                "paused_at": "TEXT",
                "idle_timeout_seconds": "INTEGER",
                "max_runtime_seconds": "INTEGER",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        self._ensure_columns(
            conn,
            "runs",
            {
                "lease_expires_at": "TEXT",
                "started_at": "TEXT",
                "finished_at": "TEXT",
                "exit_reason": "TEXT",
                "output_path": "TEXT",
                "final_response": "TEXT",
                "error": "TEXT",
                "delivery_status": "TEXT",
                "heartbeat_at": "TEXT",
                "last_activity_at": "TEXT",
                "last_activity_desc": "TEXT",
                "current_tool": "TEXT",
            },
        )
        self._ensure_columns(
            conn,
            "delivery_events",
            {
                "run_id": "TEXT",
                "adapter_key": "TEXT",
                "address": "TEXT",
                "thread_id": "TEXT",
                "origin_json": "TEXT",
            },
        )
        delivery_columns = self._table_columns(conn, "delivery_events")
        if "adapter_key" in delivery_columns:
            conn.execute("UPDATE delivery_events SET adapter_key = COALESCE(NULLIF(adapter_key, ''), target_type)")
        if "address" in delivery_columns and "target_id" in delivery_columns:
            conn.execute("UPDATE delivery_events SET address = COALESCE(address, target_id)")

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = self._table_columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _import_legacy_delivery_db_if_needed(self) -> None:
        if self.get_meta("legacy_delivery_sqlite_imported_at"):
            return
        legacy_path = get_cron_dir() / "delivery.sqlite3"
        if not legacy_path.exists() or legacy_path == self.path:
            self.set_meta("legacy_delivery_sqlite_imported_at", utc_now().isoformat())
            return
        try:
            source = sqlite3.connect(str(legacy_path))
            source.row_factory = sqlite3.Row
            source_rows = source.execute("SELECT * FROM delivery_events").fetchall()
            source_columns = {str(row["name"]) for row in source.execute("PRAGMA table_info(delivery_events)").fetchall()}
        except sqlite3.Error as exc:
            logger.warning("Could not import legacy cron delivery database %s: %s", legacy_path, exc)
            return
        finally:
            try:
                source.close()
            except Exception:
                pass

        now_text = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in source_rows:
                event_id = str(row["id"])
                exists = conn.execute("SELECT 1 FROM delivery_events WHERE id = ?", (event_id,)).fetchone()
                if exists:
                    continue
                target_type = str(row["target_type"])
                target_id = row["target_id"] if "target_id" in source_columns else None
                conn.execute(
                    """
                    INSERT INTO delivery_events (
                        id, job_id, run_id, job_name, run_at, target, target_type,
                        adapter_key, address, thread_id, origin_json, status,
                        attempt_count, next_attempt_at, last_attempt_at, last_error,
                        output_path, final_response, payload_json, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        row["job_id"],
                        row["job_name"],
                        row["run_at"],
                        row["target"],
                        target_type,
                        target_type,
                        target_id,
                        row["status"],
                        int(row["attempt_count"] or 0),
                        row["next_attempt_at"],
                        row["last_attempt_at"],
                        row["last_error"],
                        row["output_path"],
                        row["final_response"],
                        row["payload_json"],
                        row["created_at"] or now_text,
                        row["updated_at"] or now_text,
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('legacy_delivery_sqlite_imported_at', ?)",
                (now_text,),
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

    def get_scheduler_lease(self, name: str = "scheduler") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduler_leases WHERE name = ?", (name,)).fetchone()
        return None if row is None else dict(row)

    def try_acquire_scheduler_lease(
        self,
        name: str,
        owner_id: str,
        pid: int | None,
        hostname: str | None,
        now_text: str,
        expires_at: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM scheduler_leases WHERE name = ?", (name,)).fetchone()
            now_dt = _parse_time(now_text)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO scheduler_leases (
                        name, owner_id, pid, hostname, acquired_at, heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, owner_id, pid, hostname, now_text, now_text, expires_at),
                )
            elif row["owner_id"] == owner_id:
                conn.execute(
                    """
                    UPDATE scheduler_leases
                    SET pid = ?,
                        hostname = ?,
                        heartbeat_at = ?,
                        expires_at = ?
                    WHERE name = ? AND owner_id = ?
                    """,
                    (pid, hostname, now_text, expires_at, name, owner_id),
                )
            else:
                expires_dt = _parse_time(row["expires_at"])
                if expires_dt is None or (now_dt is not None and expires_dt <= now_dt):
                    conn.execute(
                        """
                        UPDATE scheduler_leases
                        SET owner_id = ?,
                            pid = ?,
                            hostname = ?,
                            acquired_at = ?,
                            heartbeat_at = ?,
                            expires_at = ?
                        WHERE name = ?
                        """,
                        (owner_id, pid, hostname, now_text, now_text, expires_at, name),
                    )
            return dict(conn.execute("SELECT * FROM scheduler_leases WHERE name = ?", (name,)).fetchone())

    def release_scheduler_lease(self, name: str, owner_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM scheduler_leases WHERE name = ? AND owner_id = ?", (name, owner_id))
        return bool(cursor.rowcount)

    def _delivery_targets_for_job(self, job: dict[str, Any], *, strict: bool) -> list[dict[str, Any]] | None:
        origin = DeliveryIdentity.from_job_origin(job.get("origin"))
        if strict:
            from cron.delivery_registry import default_delivery_registry

            validation = default_delivery_registry().validate_targets(
                job.get("deliver"),
                origin=origin,
                job=job,
            )
            if not validation.ok:
                raise ValueError(validation.error or f"unsupported delivery target: {job.get('deliver')}")
            return [target.to_json() for target in validation.targets]

        delivery_targets = job.get("delivery_targets")
        if delivery_targets is not None:
            return list(delivery_targets)

        try:
            return [
                target.to_json()
                for target in parse_delivery_targets(job.get("deliver"), origin=origin)
            ]
        except DeliveryTargetError:
            return None

    def _normalize_origin_for_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        origin = DeliveryIdentity.from_job_origin(job.get("origin"))
        return origin.to_json() if origin is not None else None

    def _job_to_row_values(self, job: dict[str, Any], now_text: str | None = None, *, strict_delivery: bool = True) -> dict[str, Any]:
        now_text = now_text or utc_now().isoformat()
        skills = list(job.get("skills") or [])
        skill = str(job.get("skill") or "").strip()
        if skill and skill not in skills:
            skills.append(skill)
        delivery_targets = self._delivery_targets_for_job(job, strict=strict_delivery)
        origin = self._normalize_origin_for_job(job)
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
            "delivery_targets_json": _json_dumps(delivery_targets) if delivery_targets is not None else None,
            "origin_json": _json_dumps(origin) if origin is not None else None,
            "workdir": job.get("workdir"),
            "script": job.get("script"),
            "context_from_json": _json_dumps(job.get("context_from")) if job.get("context_from") is not None else None,
            "skills_json": _json_dumps(skills),
            "enabled_toolsets_json": _json_dumps(job.get("enabled_toolsets")) if job.get("enabled_toolsets") is not None else None,
            "model": job.get("model"),
            "provider": job.get("provider"),
            "base_url": job.get("base_url"),
            "concurrency_key": job.get("concurrency_key") or job.get("workdir") or str(job["id"]),
            "concurrency_policy": job.get("concurrency_policy"),
            "lease_run_id": job.get("lease_run_id"),
            "lease_expires_at": job.get("lease_expires_at"),
            "paused_reason": job.get("paused_reason"),
            "paused_at": job.get("paused_at"),
            "idle_timeout_seconds": job.get("idle_timeout_seconds"),
            "max_runtime_seconds": job.get("max_runtime_seconds"),
            "created_at": job.get("created_at") or now_text,
            "updated_at": now_text,
        }

    def _can_preserve_legacy_delivery(self, existing: dict[str, Any] | None, job: dict[str, Any]) -> bool:
        return (
            existing is not None
            and existing.get("deliver") == job.get("deliver")
            and existing.get("origin") == job.get("origin")
            and existing.get("delivery_targets") == job.get("delivery_targets")
            and job.get("delivery_targets") is not None
        )

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
            "concurrency_policy": row["concurrency_policy"],
            "lease_run_id": row["lease_run_id"],
            "lease_expires_at": row["lease_expires_at"],
            "paused_reason": row["paused_reason"],
            "paused_at": row["paused_at"],
            "idle_timeout_seconds": row["idle_timeout_seconds"],
            "max_runtime_seconds": row["max_runtime_seconds"],
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

    def replace_jobs(self, jobs: list[dict[str, Any]]) -> None:
        now_text = utc_now().isoformat()
        existing_jobs = {str(job["id"]): job for job in self.list_jobs(include_disabled=True)}
        rows = []
        seen_ids: set[str] = set()
        for job in jobs:
            job_id = str(job["id"])
            if job_id in seen_ids:
                raise ValueError(f"duplicate cron job id: {job_id}")
            seen_ids.add(job_id)
            existing = existing_jobs.get(job_id)
            strict_delivery = not self._can_preserve_legacy_delivery(existing, dict(job))
            rows.append(self._job_to_row_values(dict(job), now_text=now_text, strict_delivery=strict_delivery))

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_rows = conn.execute("SELECT id FROM jobs").fetchall()
            existing_ids = {str(row["id"]) for row in existing_rows}
            for values in rows:
                columns = list(values)
                assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
                placeholders = ", ".join("?" for _ in columns)
                conn.execute(
                    f"""
                    INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})
                    ON CONFLICT(id) DO UPDATE SET {assignments}
                    """,
                    [values[column] for column in columns],
                )
            for job_id in existing_ids - seen_ids:
                conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

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
                values = self._job_to_row_values(dict(job), now_text=now_text, strict_delivery=False)
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
        strict_delivery = any(key in updates for key in ("deliver", "origin", "delivery_targets"))
        values = self._job_to_row_values(merged, strict_delivery=strict_delivery)
        assignments = ", ".join(f"{column} = ?" for column in values if column != "id")
        params = [values[column] for column in values if column != "id"] + [job_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)
        updated = self.get_job(job_id)
        if updated is None:
            raise KeyError(f"Cron job not found after update: {job_id}")
        return updated

    def update_job_delivery_error(self, job_id: str, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET last_delivery_error = ?, updated_at = ? WHERE id = ?",
                (error, utc_now().isoformat(), job_id),
            )

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

    def origin_pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM delivery_events
                WHERE adapter_key = 'origin' AND status = 'pending'
                """
            ).fetchone()
        return int(row["count"])

    def claim_due_delivery_events(self, *, limit: int = 20, adapter_keys: set[str] | None = None) -> list[dict[str, Any]]:
        now_text = utc_now().isoformat()
        now_dt = _parse_time(now_text)
        event_limit = max(0, int(limit))
        if now_dt is None or event_limit == 0:
            return []
        params: list[Any] = []
        key_filter = ""
        if adapter_keys is not None:
            if not adapter_keys:
                return []
            placeholders = ", ".join("?" for _ in adapter_keys)
            key_filter = f" AND adapter_key IN ({placeholders})"
            params.extend(sorted(adapter_keys))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT * FROM delivery_events
                WHERE status IN ('pending', 'failed')
                  {key_filter}
                ORDER BY created_at
                """,
                params,
            ).fetchall()
            claimed = []
            for row in rows:
                next_attempt_at = _parse_time(row["next_attempt_at"])
                if row["next_attempt_at"] and next_attempt_at is not None and next_attempt_at > now_dt:
                    continue
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
                if len(claimed) >= event_limit:
                    break
        return claimed

    def dead_letter_unsupported_delivery_events(
        self,
        *,
        supported_adapter_keys: set[str],
        ignored_adapter_keys: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        event_limit = max(0, int(limit))
        if event_limit == 0:
            return []
        now_dt = utc_now()
        ignored = ignored_adapter_keys or set()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('pending', 'failed')
                ORDER BY created_at
                """
            ).fetchall()
            updated = []
            for row in rows:
                adapter_key = str(row["adapter_key"] or "")
                if adapter_key in supported_adapter_keys or adapter_key in ignored:
                    continue
                next_attempt_at = _parse_time(row["next_attempt_at"])
                if row["next_attempt_at"] and next_attempt_at is not None and next_attempt_at > now_dt:
                    continue
                now_text = utc_now().isoformat()
                conn.execute(
                    """
                    UPDATE delivery_events
                    SET status = 'dead',
                        last_error = ?,
                        next_attempt_at = NULL,
                        updated_at = ?
                    WHERE id = ? AND status IN ('pending', 'failed')
                    """,
                    (f"unsupported delivery target: {row['target']}", now_text, row["id"]),
                )
                updated.append(dict(conn.execute("SELECT * FROM delivery_events WHERE id = ?", (row["id"],)).fetchone()))
                if len(updated) >= event_limit:
                    break
        return updated

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

    def delete_delivery_events(self, event_ids: list[str]) -> int:
        deleted = 0
        with self._connect() as conn:
            for event_id in event_ids:
                cursor = conn.execute(
                    "DELETE FROM delivery_events WHERE id = ?",
                    (event_id,),
                )
                deleted += int(cursor.rowcount or 0)
        return deleted

    def recover_stale_delivery_events(self, *, max_age_seconds: int = 600) -> int:
        cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM delivery_events WHERE status = 'delivering' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
        recovered = 0
        for row in rows:
            self.mark_delivery_failed(str(row["id"]), "delivery attempt abandoned")
            recovered += 1
        return recovered

    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_run(row)

    def claim_due_jobs(self, *, now_text: str, limit: int = 20) -> list[dict[str, Any]]:
        now_dt = _parse_time(now_text)
        claim_limit = max(0, int(limit))
        if now_dt is None or claim_limit == 0:
            return []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE enabled = 1
                  AND state = 'scheduled'
                  AND next_run_at IS NOT NULL
                ORDER BY created_at
                """,
            ).fetchall()
            due_jobs = []
            for row in rows:
                job = self._row_to_job(row)
                due_at = _parse_time(job.get("next_run_at"))
                if due_at is None or due_at > now_dt:
                    continue
                due_jobs.append((due_at, str(job.get("created_at") or ""), str(job["id"]), job))
            due_jobs.sort(key=lambda item: (item[0], item[1], item[2]))

            claimed = []
            for _, _, _, job in due_jobs:
                if len(claimed) >= claim_limit:
                    break
                if self._skip_missed_job_if_needed(conn, job, now_dt):
                    continue
                run_id = uuid.uuid4().hex
                now_actual = utc_now().isoformat()
                previous_attempts = conn.execute(
                    "SELECT COUNT(*) AS count FROM runs WHERE job_id = ?",
                    (job["id"],),
                ).fetchone()["count"]
                lease_expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, job_id, scheduled_for, claimed_at, lease_expires_at,
                        started_at, finished_at, attempt, status, exit_reason,
                        output_path, final_response, error, delivery_status,
                        heartbeat_at, last_activity_at, last_activity_desc, current_tool,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'claimed', NULL, NULL, NULL, NULL, NULL,
                              ?, ?, 'claimed', NULL,
                              ?, ?)
                    """,
                    (run_id, job["id"], job["next_run_at"], now_actual, lease_expires_at, int(previous_attempts) + 1, now_actual, now_actual, now_actual, now_actual),
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
                    (run_id, lease_expires_at, now_actual, job["id"]),
                )
                run = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
                claimed_job = self._row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())
                claimed.append({"job": claimed_job, "run": run})
        return claimed

    def _skip_missed_job_if_needed(self, conn: sqlite3.Connection, job: dict[str, Any], now_dt: datetime) -> bool:
        next_run_at = job.get("next_run_at")
        if not next_run_at:
            return False
        try:
            from cron.jobs import ONESHOT_GRACE_SECONDS, _parse_datetime, _recurring_grace_seconds, compute_next_run

            run_at = _parse_datetime(str(next_run_at))
        except Exception:
            return False

        schedule = job.get("schedule") or {}
        kind = schedule.get("kind")
        if kind == "once":
            missed = now_dt - run_at > timedelta(seconds=ONESHOT_GRACE_SECONDS)
            if not missed:
                return False
            next_run = None
            job_state = "completed"
            enabled = 0
        else:
            grace_seconds = _recurring_grace_seconds(schedule, run_at)
            missed = now_dt - run_at > timedelta(seconds=grace_seconds)
            if not missed:
                return False
            next_run = compute_next_run(schedule, base=now_dt)
            job_state = "scheduled"
            enabled = 1

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
                heartbeat_at, last_activity_at, last_activity_desc, current_tool,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 'skipped', 'missed_run',
                      NULL, NULL, NULL, NULL, ?, ?, 'missed_run', NULL,
                      ?, ?)
            """,
            (run_id, job["id"], next_run_at, now_actual, int(previous_attempts) + 1, now_actual, now_actual, now_actual, now_actual),
        )
        conn.execute(
            """
            UPDATE jobs
            SET state = ?, enabled = ?, next_run_at = ?, lease_run_id = NULL,
                lease_expires_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (job_state, enabled, next_run, now_actual, job["id"]),
        )
        return True

    def mark_run_started(self, run_id: str) -> dict[str, Any] | None:
        now_text = utc_now().isoformat()
        now_dt = _parse_time(now_text)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT runs.status AS run_status, runs.job_id AS job_id,
                       jobs.state AS job_state, jobs.lease_run_id AS lease_run_id,
                       jobs.lease_expires_at AS lease_expires_at
                FROM runs
                JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.id = ?
                """,
                (run_id,),
            ).fetchone()
            lease_expires_at = _parse_time(row["lease_expires_at"]) if row else None
            if (
                row is None
                or row["run_status"] != "claimed"
                or row["job_state"] != "running"
                or row["lease_run_id"] != run_id
                or lease_expires_at is None
                or now_dt is None
                or lease_expires_at <= now_dt
            ):
                if row is not None and row["lease_run_id"] == run_id:
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'abandoned', finished_at = ?,
                            exit_reason = 'lease_expired', updated_at = ?
                        WHERE id = ? AND status = 'claimed'
                        """,
                        (now_text, now_text, run_id),
                    )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET state = 'scheduled', lease_run_id = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE id = ? AND lease_run_id = ?
                        """,
                        (now_text, row["job_id"], run_id),
                    )
                return None
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'running', started_at = ?,
                    heartbeat_at = ?, last_activity_at = ?, last_activity_desc = 'started',
                    current_tool = NULL, updated_at = ?
                WHERE id = ? AND status = 'claimed'
                """,
                (now_text, now_text, now_text, now_text, run_id),
            )
        if not cursor.rowcount:
            return None
        return self.get_run(run_id)

    def update_run_activity(
        self,
        run_id: str,
        *,
        heartbeat: bool = True,
        activity: bool = False,
        last_activity_desc: str | None = None,
        current_tool: str | None = None,
    ) -> dict[str, Any] | None:
        now_text = utc_now().isoformat()
        assignments: list[str] = []
        values: list[Any] = []
        if heartbeat or activity:
            assignments.append("heartbeat_at = ?")
            values.append(now_text)
        if activity:
            assignments.append("last_activity_at = ?")
            values.append(now_text)
        if last_activity_desc is not None:
            assignments.append("last_activity_desc = ?")
            values.append(last_activity_desc)
        if current_tool is not None or activity:
            assignments.append("current_tool = ?")
            values.append(current_tool)
        if not assignments:
            return self.get_run(run_id)
        assignments.append("updated_at = ?")
        values.append(now_text)
        values.append(run_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE runs
                SET {", ".join(assignments)}
                WHERE id = ?
                  AND status IN ('claimed', 'running')
                """,
                values,
            )
        if not cursor.rowcount:
            return None
        return self.get_run(run_id)

    def run_owns_lease(self, run_id: str, *, now_text: str | None = None) -> bool:
        run = self.get_run(run_id)
        now_text = now_text or utc_now().isoformat()
        now_dt = _parse_time(now_text)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT lease_run_id, lease_expires_at, state
                FROM jobs
                WHERE id = ?
                """,
                (run["job_id"],),
            ).fetchone()
        return (
            row is not None
            and row["state"] == "running"
            and row["lease_run_id"] == run_id
            and row["lease_expires_at"] is not None
            and now_dt is not None
            and (_parse_time(row["lease_expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > now_dt
        )

    def delivery_error_for_run(self, run_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_error FROM delivery_events
                WHERE run_id = ?
                  AND status IN ('failed', 'dead')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else str(row["last_error"] or "delivery failed")

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
        delivery_error: str | None = None,
        run_status: str | None = None,
        exit_reason: str | None = None,
    ) -> dict[str, Any]:
        computed_status = run_status or ("succeeded" if success else "failed")
        job_state = "completed" if completed else "scheduled"
        job_id: str
        terminal_run: dict[str, Any] | None = None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_text = utc_now().isoformat()
            now_dt = _parse_time(now_text)
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            run = dict(run_row)
            job_id = str(run["job_id"])
            if run.get("status") in {"succeeded", "failed", "skipped", "abandoned"}:
                terminal_run = run
            else:
                owner = conn.execute(
                    "SELECT lease_run_id, lease_expires_at, state FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if (
                    owner is None
                    or owner["state"] != "running"
                    or owner["lease_run_id"] != run_id
                    or owner["lease_expires_at"] is None
                    or now_dt is None
                    or (_parse_time(owner["lease_expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= now_dt
                ):
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'abandoned', finished_at = ?, output_path = ?,
                            final_response = ?, error = ?, exit_reason = 'late_completion',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now_text, output_path, final_response, error, now_text, run_id),
                    )
                    if owner is not None and owner["lease_run_id"] == run_id:
                        conn.execute(
                            """
                            UPDATE jobs
                            SET state = 'scheduled', lease_run_id = NULL,
                                lease_expires_at = NULL, updated_at = ?
                            WHERE id = ? AND lease_run_id = ?
                            """,
                            (now_text, job_id, run_id),
                        )
                else:
                    job_row = conn.execute(
                        "SELECT repeat_json FROM jobs WHERE id = ? AND lease_run_id = ? AND state = 'running'",
                        (job_id, run_id),
                    ).fetchone()
                    repeat = _json_loads(job_row["repeat_json"], {"times": None, "completed": 0}) if job_row else {"times": None, "completed": 0}
                    repeat["completed"] = int(repeat.get("completed") or 0) + 1
                    updated_run = conn.execute(
                        """
                        UPDATE runs
                        SET status = ?, finished_at = ?, output_path = ?, final_response = ?,
                            error = ?, exit_reason = ?, heartbeat_at = ?, last_activity_at = ?,
                            last_activity_desc = 'completed', current_tool = NULL, updated_at = ?
                        WHERE id = ? AND status IN ('claimed', 'running')
                        """,
                        (
                            computed_status,
                            now_text,
                            output_path,
                            final_response,
                            error,
                            None if success else exit_reason,
                            now_text,
                            now_text,
                            now_text,
                            run_id,
                        ),
                    )
                    if updated_run.rowcount:
                        conn.execute(
                            """
                            UPDATE jobs
                            SET state = ?, enabled = ?, next_run_at = ?, repeat_json = ?,
                                lease_run_id = NULL,
                                lease_expires_at = NULL, last_run_at = ?, last_status = ?,
                                last_error = ?, last_delivery_error = ?, updated_at = ?
                            WHERE id = ? AND lease_run_id = ? AND state = 'running'
                            """,
                            (
                                job_state,
                                0 if completed else 1,
                                next_run_at,
                                _json_dumps(repeat),
                                now_text,
                                "ok" if success else "error",
                                None if success else error,
                                delivery_error,
                                now_text,
                                job_id,
                                run_id,
                            ),
                        )
        if terminal_run is not None:
            return {"run": terminal_run, "job": self.get_job(job_id)}
        return {"run": self.get_run(run_id), "job": self.get_job(job_id)}

    def update_run_delivery_status(self, run_id: str) -> str | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status FROM delivery_events WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if not rows:
                delivery_status = None
            else:
                statuses = {str(row["status"]) for row in rows}
                if "dead" in statuses:
                    delivery_status = "failed"
                elif "failed" in statuses:
                    delivery_status = "retrying"
                elif statuses <= {"delivered"}:
                    delivery_status = "delivered"
                elif "delivered" in statuses:
                    delivery_status = "partial"
                else:
                    delivery_status = "pending"
            conn.execute(
                "UPDATE runs SET delivery_status = ?, updated_at = ? WHERE id = ?",
                (delivery_status, utc_now().isoformat(), run_id),
            )
        return delivery_status

    def recover_expired_leases(self, *, now_text: str) -> int:
        now_actual = utc_now().isoformat()
        now_dt = _parse_time(now_text)
        if now_dt is None:
            return 0
        recovered = 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.id AS run_id, runs.job_id AS job_id,
                       jobs.lease_expires_at AS lease_expires_at
                FROM runs
                JOIN jobs ON jobs.lease_run_id = runs.id
                WHERE jobs.state = 'running'
                  AND jobs.lease_expires_at IS NOT NULL
                  AND runs.status IN ('claimed', 'running')
                """,
            ).fetchall()
            for row in rows:
                lease_expires_at = _parse_time(row["lease_expires_at"])
                if lease_expires_at is None or lease_expires_at > now_dt:
                    continue
                conn.execute(
                    "UPDATE runs SET status = 'abandoned', finished_at = ?, exit_reason = 'lease_expired', updated_at = ? WHERE id = ?",
                    (now_actual, now_actual, row["run_id"]),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'scheduled', lease_run_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (now_actual, row["job_id"]),
                )
                recovered += 1
        return recovered

    def runs_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE job_id = ? ORDER BY created_at", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    # --- Poll State Methods ---

    def _init_poll_state_schema(self) -> None:
        """Initialize the poll_state table if it doesn't exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_state (
                    adapter_key TEXT PRIMARY KEY,
                    cursor TEXT,
                    updated_at TEXT
                )
                """
            )

    def get_poll_state(self, adapter_key: str) -> dict[str, Any] | None:
        """Retrieve the persisted poll state for an adapter.

        Args:
            adapter_key: The adapter key to look up.

        Returns:
            dict with adapter_key, cursor, updated_at, or None if not found.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM poll_state WHERE adapter_key = ?",
                (adapter_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "adapter_key": row["adapter_key"],
            "cursor": row["cursor"],
            "updated_at": row["updated_at"],
        }

    def save_poll_state(self, adapter_key: str, cursor: str | None) -> None:
        """Persist the poll state for an adapter.

        Args:
            adapter_key: The adapter key.
            cursor: The next cursor value to save, or None if no more pages.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO poll_state (adapter_key, cursor, updated_at) VALUES (?, ?, ?)",
                (adapter_key, cursor, utc_now().isoformat()),
            )

    def resolve_job_timeouts(self, job: dict[str, Any]) -> dict[str, int | None]:
        """Resolve idle_timeout_seconds and max_runtime_seconds for a job.

        Job-level values take precedence over AGENT_CRON_TIMEOUT env var.
        AGENT_CRON_TIMEOUT defaults to 600 seconds.
        """
        def positive_or_zero(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None

        idle = positive_or_zero(job.get("idle_timeout_seconds"))
        if idle is None:
            raw = os.getenv("AGENT_CRON_TIMEOUT", "600")
            try:
                idle = int(raw)
            except ValueError:
                idle = 600
        max_runtime = positive_or_zero(job.get("max_runtime_seconds"))
        return {
            "idle_timeout_seconds": idle if idle is not None and idle > 0 else None,
            "max_runtime_seconds": max_runtime if max_runtime is not None and max_runtime > 0 else None,
        }

    def list_next_due_jobs(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """List upcoming scheduled jobs ordered by next_run_at."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE enabled = 1
                  AND state = 'scheduled'
                  AND next_run_at IS NOT NULL
                ORDER BY next_run_at, created_at, id
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_running_runs(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """List active runs (claimed or running) with job metadata."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.id AS run_id, runs.*, jobs.name AS job_name, jobs.next_run_at,
                       jobs.id AS job_id, jobs.idle_timeout_seconds, jobs.max_runtime_seconds
                FROM runs
                JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.status IN ('claimed', 'running')
                ORDER BY COALESCE(runs.started_at, runs.claimed_at), runs.id
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_failed_run(self) -> dict[str, Any] | None:
        """Get the most recent failed or abandoned run."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT runs.id AS run_id, runs.*, jobs.name AS job_name, jobs.id AS job_id
                FROM runs
                JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.status IN ('failed', 'abandoned')
                ORDER BY COALESCE(runs.finished_at, runs.updated_at) DESC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else dict(row)

    def list_stale_running_runs(self, *, now_text: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Detect runs that have exceeded lease, heartbeat, or idle timeout thresholds."""
        now_dt = _parse_time(now_text or utc_now().isoformat())
        if now_dt is None:
            return []
        stale: list[dict[str, Any]] = []
        for row in self.list_running_runs(limit=100):
            job_timeout = self.resolve_job_timeouts(row)
            reason = None
            heartbeat_at = _parse_time(row.get("heartbeat_at"))
            activity_at = _parse_time(row.get("last_activity_at") or row.get("started_at") or row.get("claimed_at"))
            lease_expires_at = _parse_time(row.get("lease_expires_at"))
            if lease_expires_at is not None and lease_expires_at <= now_dt:
                reason = "lease_expired"
            elif heartbeat_at is not None and (now_dt - heartbeat_at).total_seconds() > 300:
                reason = "heartbeat_stale"
            elif job_timeout["idle_timeout_seconds"] and activity_at is not None:
                if (now_dt - activity_at).total_seconds() > job_timeout["idle_timeout_seconds"]:
                    reason = "idle_timeout_exceeded"
            if reason:
                enriched = dict(row)
                enriched["stale_reason"] = reason
                stale.append(enriched)
            if len(stale) >= max(1, int(limit)):
                break
        return stale
