from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.contracts import InboundEvent


SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_inbox (
  id              TEXT PRIMARY KEY,
  platform        TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  chat_id         TEXT NOT NULL,
  thread_id       TEXT,
  sender_id       TEXT,
  sender_name     TEXT,
  text            TEXT NOT NULL DEFAULT '',
  raw_json        TEXT NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  claimed_at      TEXT,
  next_attempt_at TEXT,
  last_error      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_idempotent
  ON gateway_inbox (platform, event_id);

CREATE INDEX IF NOT EXISTS idx_inbox_status_claimed
  ON gateway_inbox (status, claimed_at);
"""


@dataclass(frozen=True)
class InboxItem:
    id: str
    platform: str
    event_id: str
    event_type: str
    chat_id: str
    thread_id: str | None
    sender_id: str | None
    sender_name: str | None
    text: str
    raw_json: dict[str, Any]
    status: str
    attempts: int
    claimed_at: str | None
    next_attempt_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class GatewayInboxStore:
    DEFAULT_STALE_SECONDS = 300
    DEFAULT_MAX_ATTEMPTS = 3

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_id() -> str:
        return f"ibx_{uuid.uuid4().hex[:12]}"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _row_to_item(self, row: sqlite3.Row) -> InboxItem:
        raw = row["raw_json"]
        raw_parsed: dict[str, Any] = json.loads(raw) if raw else {}
        thread_id_val = row["thread_id"]

        return InboxItem(
            id=row["id"],
            platform=row["platform"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            chat_id=row["chat_id"],
            thread_id=thread_id_val if thread_id_val else None,
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=row["text"],
            raw_json=raw_parsed,
            status=row["status"],
            attempts=row["attempts"],
            claimed_at=row["claimed_at"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def enqueue(self, event: InboundEvent) -> str:
        now = self.now()
        row_id = self.new_id()
        raw_json = json.dumps(event.raw, ensure_ascii=False)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_inbox
                  (id, platform, event_id, event_type, chat_id, thread_id,
                   sender_id, sender_name, text, raw_json, status,
                   attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                ON CONFLICT(platform, event_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (
                    row_id,
                    event.platform,
                    event.event_id,
                    event.event_type,
                    event.chat_id,
                    event.thread_id,
                    event.sender_id,
                    event.sender_name,
                    event.text,
                    raw_json,
                    now,
                    now,
                ),
            )

            actual_row = conn.execute(
                "SELECT id FROM gateway_inbox WHERE platform = ? AND event_id = ?",
                (event.platform, event.event_id),
            ).fetchone()

        return actual_row["id"]

    def claim_due(self, *, limit: int = 10) -> list[InboxItem]:
        now = self.now()

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbox
                SET status = 'processing',
                    claimed_at = ?,
                    updated_at = ?
                WHERE id IN (
                    SELECT id FROM gateway_inbox
                    WHERE (status = 'pending')
                       OR (status = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?)
                    ORDER BY created_at
                    LIMIT ?
                )
                """,
                (now, now, now, limit),
            )

            rows = conn.execute(
                """
                SELECT * FROM gateway_inbox
                WHERE status = 'processing' AND claimed_at = ?
                ORDER BY created_at
                """,
                (now,),
            ).fetchall()

        return [self._row_to_item(row) for row in rows]

    def complete(self, id: str) -> None:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbox
                SET status = 'succeeded',
                    next_attempt_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, id),
            )

    def fail(
        self,
        id: str,
        error: str,
        *,
        retry_delays: list[int] | None = None,
        max_attempts: int | None = None,
    ) -> None:
        max_attempts = max_attempts if max_attempts is not None else self.DEFAULT_MAX_ATTEMPTS

        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM gateway_inbox WHERE id = ?",
                (id,),
            ).fetchone()
            if row is None:
                return

            new_attempts = row["attempts"] + 1
            now = self.now()

            if new_attempts >= max_attempts:
                new_status = "dead"
                next_at = None
            else:
                new_status = "failed"
                delay_seconds = (retry_delays or [60])[0]
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()

            conn.execute(
                """
                UPDATE gateway_inbox
                SET status = ?,
                    attempts = ?,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_status, new_attempts, error, next_at, now, id),
            )

    def recover_stale_processing(self, *, stale_seconds: int | None = None) -> int:
        stale_seconds = stale_seconds if stale_seconds is not None else self.DEFAULT_STALE_SECONDS
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        now = self.now()

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE gateway_inbox
                SET status = 'failed',
                    next_attempt_at = ?,
                    last_error = 'stale processing recovered',
                    updated_at = ?
                WHERE status = 'processing' AND claimed_at < ?
                """,
                (now, now, cutoff),
            )

        return cursor.rowcount

    def get(self, id: str) -> InboxItem | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_inbox WHERE id = ?",
                (id,),
            ).fetchone()

        if row is None:
            return None
        return self._row_to_item(row)

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM gateway_inbox
                GROUP BY status
                """,
            ).fetchall()

        counts = {status: 0 for status in ("pending", "processing", "succeeded", "failed", "dead")}
        for row in rows:
            status_val = row["status"]
            if status_val in counts:
                counts[status_val] = row["cnt"]

        return counts
