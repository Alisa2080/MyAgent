from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_cli.history import filter_transcript_messages


SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_sessions (
  session_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  workdir TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  last_message_preview TEXT,
  updated_seq INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    title: str
    created_at: str
    updated_at: str
    workdir: str
    model: str | None
    status: str
    last_message_preview: str | None


@dataclass(frozen=True)
class SessionListItem(SessionRecord):
    first_user_prompt_preview: str


class SessionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_session_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def title_from_message(message: str, max_length: int = 60) -> str:
        normalized = re.sub(r"\s+", " ", message).strip()
        if not normalized:
            return "New session"
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(cli_sessions)").fetchall()
            }
            if "updated_seq" not in columns:
                conn.execute(
                    "ALTER TABLE cli_sessions "
                    "ADD COLUMN updated_seq INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _next_updated_seq(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(updated_seq), 0) + 1 FROM cli_sessions"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _record_from_row(row: sqlite3.Row | tuple) -> SessionRecord:
        return SessionRecord(
            session_id=row[0],
            title=row[1],
            created_at=row[2],
            updated_at=row[3],
            workdir=row[4],
            model=row[5],
            status=row[6],
            last_message_preview=row[7],
        )

    @staticmethod
    def _normalize_preview_text(text: str, *, max_length: int = 80) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."

    @staticmethod
    def _first_user_prompt_preview(checkpointer: Any, session_id: str) -> str | None:
        if checkpointer is None:
            return None
        from agent_cli.history import load_thread_transcript

        transcript = load_thread_transcript(checkpointer, session_id)
        for message in transcript:
            if message.role != "user":
                continue
            preview = SessionStore._normalize_preview_text(message.content)
            if preview:
                return preview
        return None

    def delete_session(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cli_sessions WHERE session_id = ?", (session_id,))

    def list_session_items(
        self, *, checkpointer: Any = None, limit: int = 20
    ) -> list[SessionListItem]:
        rows = self.list_sessions(limit=limit)
        items: list[SessionListItem] = []
        empty_session_ids: list[str] = []
        for row in rows:
            preview = self._first_user_prompt_preview(checkpointer, row.session_id)
            if not preview:
                empty_session_ids.append(row.session_id)
                continue
            items.append(
                SessionListItem(
                    session_id=row.session_id,
                    title=row.title,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    workdir=row.workdir,
                    model=row.model,
                    status=row.status,
                    last_message_preview=row.last_message_preview,
                    first_user_prompt_preview=preview,
                )
            )
        for session_id in empty_session_ids:
            self.delete_session(session_id)
        return items

    def create_session(
        self,
        *,
        workdir: str,
        model: str | None,
        title: str = "New session",
        session_id: str | None = None,
    ) -> SessionRecord:
        now = self.now()
        sid = session_id or self.new_session_id()
        with self.connect() as conn:
            updated_seq = self._next_updated_seq(conn)
            conn.execute(
                """
                INSERT INTO cli_sessions (
                    session_id, title, created_at, updated_at,
                    workdir, model, status, last_message_preview, updated_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?)
                """,
                (sid, title, now, now, workdir, model, updated_seq),
            )
        record = self.get_session(sid)
        if record is None:
            raise RuntimeError(f"failed to create session {sid}")
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at,
                       workdir, model, status, last_message_preview
                FROM cli_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._record_from_row(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at,
                       workdir, model, status, last_message_preview
                FROM cli_sessions
                ORDER BY updated_seq DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def touch_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        last_message_preview: str | None = None,
    ) -> None:
        existing = self.get_session(session_id)
        if existing is None:
            raise ValueError(f"unknown session: {session_id}")
        new_title = title if title is not None else existing.title
        preview = (
            last_message_preview
            if last_message_preview is not None
            else existing.last_message_preview
        )
        with self.connect() as conn:
            updated_seq = self._next_updated_seq(conn)
            conn.execute(
                """
                UPDATE cli_sessions
                SET title = ?, updated_at = ?, last_message_preview = ?, updated_seq = ?
                WHERE session_id = ?
                """,
                (new_title, self.now(), preview, updated_seq, session_id),
            )

    def get_or_create_session(
        self,
        *,
        session_id: str,
        workdir: str,
        model: str | None,
        title: str = "New session",
    ) -> SessionRecord:
        existing = self.get_session(session_id)
        if existing is not None:
            return existing
        return self.create_session(
            workdir=workdir,
            model=model,
            title=title,
            session_id=session_id,
        )

    def update_session(self, session_id: str, *, title: str | None = None) -> None:
        existing = self.get_session(session_id)
        if existing is None:
            raise ValueError(f"unknown session: {session_id}")
        self.touch_session(session_id, title=title)
