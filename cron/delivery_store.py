from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from cron.paths import ensure_cron_dirs, get_cron_dir, secure_file

STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}
RETRY_DELAYS = (60, 300, 900, 3600, 21600)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_delivery_db_path() -> Path:
    return get_cron_dir() / "delivery.sqlite3"


class DeliveryStore:
    def __init__(self, path: Path | None = None, *, max_attempts: int = 5) -> None:
        ensure_cron_dirs()
        self.path = path or get_delivery_db_path()
        self.max_attempts = max(1, int(max_attempts))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_events (
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_events(status, next_attempt_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_origin ON delivery_events(target_type, target_id, status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_job ON delivery_events(job_id, created_at)")
        secure_file(self.path)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def enqueue(
        self,
        *,
        job_id: str | None,
        job_name: str | None,
        run_at: str | None,
        target: str,
        target_type: str,
        target_id: str | None,
        final_response: str | None,
        output_path: str | None,
        payload: dict[str, Any],
        status: str = "pending",
        last_error: str | None = None,
    ) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        now = utc_now().isoformat()
        if status not in STATUSES:
            raise ValueError(f"invalid delivery status: {status}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events (
                    id, job_id, job_name, run_at, target, target_type, target_id,
                    status, attempt_count, next_attempt_at, last_attempt_at,
                    last_error, output_path, final_response, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, job_id, job_name, run_at, target, target_type, target_id,
                    status, now if status in {"pending", "failed"} else None,
                    last_error, output_path, final_response,
                    json.dumps(payload, ensure_ascii=False), now, now,
                ),
            )
        return self.get(event_id)

    def get(self, event_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM delivery_events WHERE id = ?", (event_id,)).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(event_id)
        return result

    def update_event(self, event_id: str, **updates: Any) -> dict[str, Any]:
        if not updates:
            return self.get(event_id)
        if "status" in updates and updates["status"] not in STATUSES:
            raise ValueError(f"invalid delivery status: {updates['status']}")
        updates.setdefault("updated_at", utc_now().isoformat())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [event_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE delivery_events SET {assignments} WHERE id = ?", values)
        return self.get(event_id)

    def claim_due(
        self,
        *,
        limit: int = 20,
        target_types: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        now = utc_now().isoformat()
        capped_limit = max(0, int(limit))
        if capped_limit <= 0:
            return []

        target_filter = ""
        params: list[Any] = [now]
        if target_types is not None:
            normalized_targets = [str(item) for item in target_types]
            if not normalized_targets:
                return []
            placeholders = ", ".join("?" for _ in normalized_targets)
            target_filter = f" AND target_type IN ({placeholders})"
            params.extend(normalized_targets)
        params.append(capped_limit)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT * FROM delivery_events
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  {target_filter}
                ORDER BY created_at
                LIMIT ?
                """,
                params,
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                event = dict(row)
                cursor = conn.execute(
                    """
                    UPDATE delivery_events
                    SET status = 'delivering',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('pending', 'failed')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    """,
                    (now, now, event["id"], now),
                )
                if cursor.rowcount:
                    claimed.append(dict(conn.execute("SELECT * FROM delivery_events WHERE id = ?", (event["id"],)).fetchone()))
        return claimed

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        return self.update_event(event_id, status="delivered", last_error=None, next_attempt_at=None)

    def mark_failed(self, event_id: str, error: str) -> dict[str, Any]:
        event = self.get(event_id)
        if int(event["attempt_count"]) >= self.max_attempts:
            return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)
        index = max(0, min(int(event["attempt_count"]) - 1, len(RETRY_DELAYS) - 1))
        next_attempt = utc_now() + timedelta(seconds=RETRY_DELAYS[index])
        return self.update_event(
            event_id,
            status="failed",
            last_error=error,
            next_attempt_at=next_attempt.isoformat(),
        )

    def mark_dead(self, event_id: str, error: str) -> dict[str, Any]:
        return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)

    def pending_origin_events(self, thread_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE target_type = 'origin'
                  AND target_id = ?
                  AND status IN ('pending', 'failed')
                ORDER BY created_at
                LIMIT ?
                """,
                (str(thread_id), max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        stats = {status: 0 for status in STATUSES}
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_events GROUP BY status").fetchall()
        for row in rows:
            stats[str(row["status"])] = int(row["count"])
        return stats

    def recent_errors(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('failed', 'dead') OR last_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stale_delivering(self, *, max_age_seconds: int = 600) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery_events WHERE status = 'delivering' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]
