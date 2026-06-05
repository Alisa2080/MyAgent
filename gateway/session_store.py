from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_sessions (
  session_id  TEXT PRIMARY KEY,
  platform    TEXT NOT NULL,
  chat_id     TEXT NOT NULL,
  thread_id   TEXT,
  sender_id   TEXT,
  sender_name TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  last_event_id TEXT,
  last_message_preview TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gw_session_identity
  ON gateway_sessions (platform, chat_id, COALESCE(thread_id, ''));

CREATE TABLE IF NOT EXISTS gateway_messages (
  message_id  TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES gateway_sessions(session_id),
  direction   TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
  platform    TEXT NOT NULL,
  event_id    TEXT,
  text        TEXT NOT NULL DEFAULT '',
  raw         TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gw_messages_session
  ON gateway_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS gateway_inbound_events (
  platform TEXT NOT NULL,
  event_id TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'succeeded',
  PRIMARY KEY (platform, event_id)
);

CREATE TABLE IF NOT EXISTS gateway_pending_interrupts (
  session_id TEXT PRIMARY KEY REFERENCES gateway_sessions(session_id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
"""


@dataclass(frozen=True)
class GatewaySession:
    session_id: str
    platform: str
    chat_id: str
    thread_id: str | None
    sender_id: str | None
    sender_name: str | None
    status: str
    last_event_id: str | None
    last_message_preview: str | None


@dataclass(frozen=True)
class GatewayMessage:
    message_id: str
    session_id: str
    direction: str
    platform: str
    event_id: str | None
    text: str
    raw: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class GatewayPendingInterrupt:
    session_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str


class GatewaySessionStore:
    DEFAULT_PROCESSING_STALE_SECONDS = 300

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(gateway_inbound_events)").fetchall()
            }
            if "status" not in columns:
                conn.execute(
                    "ALTER TABLE gateway_inbound_events ADD COLUMN status TEXT NOT NULL DEFAULT 'succeeded'"
                )

    @staticmethod
    def _session_from_row(row: sqlite3.Row | tuple) -> GatewaySession:
        thread_id_val = row["thread_id"]
        # Convert None or empty string "" back to None for type consistency
        thread_id_clean = thread_id_val if thread_id_val else None

        return GatewaySession(
            session_id=row["session_id"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            thread_id=thread_id_clean,
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            status=row["status"],
            last_event_id=row["last_event_id"],
            last_message_preview=row["last_message_preview"],
        )

    def get_or_create_session(
        self,
        *,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        sender_id: str,
        sender_name: str,
    ) -> GatewaySession:
        now = self.now()
        session_id = self.new_id("gw")

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_sessions
                    (session_id, platform, chat_id, thread_id,
                     sender_id, sender_name, created_at, updated_at,
                     status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(platform, chat_id, COALESCE(thread_id, '')) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, platform, chat_id, thread_id,
                 sender_id, sender_name, now, now),
            )

            row = conn.execute(
                """
                SELECT session_id, platform, chat_id, thread_id,
                       sender_id, sender_name, status,
                       last_event_id, last_message_preview
                FROM gateway_sessions
                WHERE platform = ? AND chat_id = ? AND COALESCE(thread_id, '') = COALESCE(?, '')
                """,
                (platform, chat_id, thread_id),
            ).fetchone()

        return self._session_from_row(row)

    def record_message(
        self,
        session_id: str,
        *,
        direction: str,
        platform: str,
        event_id: str | None,
        text: str,
        raw: dict[str, Any],
    ) -> None:
        now = self.now()
        message_id = self.new_id("msg")
        raw_json = json.dumps(raw, ensure_ascii=False)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_messages
                    (message_id, session_id, direction, platform,
                     event_id, text, raw, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, session_id, direction, platform,
                 event_id, text, raw_json, now),
            )
            conn.execute(
                """
                UPDATE gateway_sessions
                SET updated_at = ?,
                    last_event_id = COALESCE(?, last_event_id),
                    last_message_preview = ?
                WHERE session_id = ?
                """,
                (now, event_id, text, session_id),
            )

    def list_messages(self, session_id: str) -> list[GatewayMessage]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, session_id, direction, platform,
                       event_id, text, raw, created_at
                FROM gateway_messages
                WHERE session_id = ?
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()

        return [
            GatewayMessage(
                message_id=row["message_id"],
                session_id=row["session_id"],
                direction=row["direction"],
                platform=row["platform"],
                event_id=row["event_id"],
                text=row["text"],
                raw=json.loads(row["raw"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def claim_event(self, platform: str, event_id: str) -> bool:
        now = self.now()
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_inbound_events (platform, event_id, claimed_at, status)
                    VALUES (?, ?, ?, 'succeeded')
                    """,
                    (platform, event_id, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def has_event(self, platform: str, event_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM gateway_inbound_events
                WHERE platform = ? AND event_id = ?
                """,
                (platform, event_id),
            ).fetchone()
        return row is not None

    def begin_event_processing(
        self,
        platform: str,
        event_id: str,
        *,
        stale_after_seconds: int = DEFAULT_PROCESSING_STALE_SECONDS,
    ) -> bool:
        now = self.now()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_inbound_events (platform, event_id, claimed_at, status)
                    VALUES (?, ?, ?, 'processing')
                    """,
                    (platform, event_id, now),
                )
                return True
            except sqlite3.IntegrityError:
                cursor = conn.execute(
                    """
                    UPDATE gateway_inbound_events
                    SET claimed_at = ?, status = 'processing'
                    WHERE platform = ?
                      AND event_id = ?
                      AND status = 'processing'
                      AND claimed_at < ?
                    """,
                    (now, platform, event_id, cutoff),
                )
                return cursor.rowcount > 0

    def complete_event(self, platform: str, event_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbound_events
                SET status = 'succeeded', claimed_at = ?
                WHERE platform = ? AND event_id = ?
                """,
                (self.now(), platform, event_id),
            )

    def release_event(self, platform: str, event_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM gateway_inbound_events
                WHERE platform = ? AND event_id = ? AND status = 'processing'
                """,
                (platform, event_id),
            )

    @staticmethod
    def _pending_interrupt_from_row(row: sqlite3.Row) -> GatewayPendingInterrupt:
        return GatewayPendingInterrupt(
            session_id=row["session_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_pending_interrupt(self, session_id: str, *, kind: str, payload: dict[str, Any]) -> None:
        now = self.now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_pending_interrupts
                    (session_id, kind, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    kind = excluded.kind,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (session_id, kind, payload_json, now, now),
            )

    def get_pending_interrupt(self, session_id: str) -> GatewayPendingInterrupt | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, kind, payload, created_at, updated_at
                FROM gateway_pending_interrupts
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._pending_interrupt_from_row(row) if row is not None else None

    def clear_pending_interrupt(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM gateway_pending_interrupts WHERE session_id = ?",
                (session_id,),
            )
