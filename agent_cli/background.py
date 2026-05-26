from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TASK_STATUSES = {
    "queued",
    "running",
    "waiting_approval",
    "stopping",
    "stopped",
    "completed",
    "failed",
}
ACTIVE_TASK_STATUSES = {"queued", "running", "waiting_approval", "stopping"}
TERMINAL_TASK_STATUSES = {"stopped", "completed", "failed"}


TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_background_tasks (
  task_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  prompt_preview TEXT,
  last_result_preview TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  pending_steer_count INTEGER NOT NULL DEFAULT 0
);
"""


STEER_SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_background_steers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
"""


@dataclass(frozen=True)
class BackgroundTaskRecord:
    task_id: str
    session_id: str
    title: str
    status: str
    prompt_preview: str | None
    last_result_preview: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool
    pending_steer_count: int


@dataclass(frozen=True)
class BackgroundSteerRecord:
    id: int
    task_id: str
    message: str
    status: str
    created_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class BackgroundNotification:
    kind: str
    task_id: str
    session_id: str
    status: str
    message: str


class BackgroundTaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(TASK_SCHEMA)
            conn.execute(STEER_SCHEMA)

    @staticmethod
    def _task_from_row(row: sqlite3.Row | tuple) -> BackgroundTaskRecord:
        return BackgroundTaskRecord(
            task_id=row[0],
            session_id=row[1],
            title=row[2],
            status=row[3],
            prompt_preview=row[4],
            last_result_preview=row[5],
            last_error=row[6],
            created_at=row[7],
            updated_at=row[8],
            started_at=row[9],
            finished_at=row[10],
            cancel_requested=bool(row[11]),
            pending_steer_count=int(row[12]),
        )

    @staticmethod
    def _steer_from_row(row: sqlite3.Row | tuple) -> BackgroundSteerRecord:
        return BackgroundSteerRecord(
            id=int(row[0]),
            task_id=row[1],
            message=row[2],
            status=row[3],
            created_at=row[4],
            consumed_at=row[5],
        )

    def create_task(
        self,
        *,
        task_id: str,
        session_id: str,
        title: str,
        prompt_preview: str,
    ) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cli_background_tasks (
                    task_id, session_id, title, status, prompt_preview,
                    last_result_preview, last_error, created_at, updated_at,
                    started_at, finished_at, cancel_requested, pending_steer_count
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, NULL, 0, 0)
                """,
                (task_id, session_id, title, prompt_preview, now, now),
            )
        record = self.get_task(task_id)
        if record is None:
            raise RuntimeError(f"failed to create background task {task_id}")
        return record

    def get_task(self, task_id: str) -> BackgroundTaskRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT task_id, session_id, title, status, prompt_preview,
                       last_result_preview, last_error, created_at, updated_at,
                       started_at, finished_at, cancel_requested, pending_steer_count
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 20,
    ) -> list[BackgroundTaskRecord]:
        where = ""
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(sorted(statuses))
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task_id, session_id, title, status, prompt_preview,
                       last_result_preview, last_error, created_at, updated_at,
                       started_at, finished_at, cancel_requested, pending_steer_count
                FROM cli_background_tasks
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def set_status(
        self,
        task_id: str,
        status: str,
        *,
        last_result_preview: str | None = None,
        last_error: str | None = None,
        cancel_requested: bool | None = None,
        finished: bool = False,
    ) -> BackgroundTaskRecord:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid background task status: {status}")
        existing = self.get_task(task_id)
        if existing is None:
            raise ValueError(f"unknown background task: {task_id}")
        now = self.now()
        finished_at = now if finished else existing.finished_at
        cancel_value = (
            int(cancel_requested)
            if cancel_requested is not None
            else int(existing.cancel_requested)
        )
        result_preview = (
            last_result_preview
            if last_result_preview is not None
            else existing.last_result_preview
        )
        error_value = last_error if last_error is not None else existing.last_error
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET status = ?, updated_at = ?, finished_at = ?,
                    last_result_preview = ?, last_error = ?, cancel_requested = ?
                WHERE task_id = ?
                """,
                (
                    status,
                    now,
                    finished_at,
                    result_preview,
                    error_value,
                    cancel_value,
                    task_id,
                ),
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to update background task {task_id}")
        return updated

    def mark_started(self, task_id: str) -> BackgroundTaskRecord:
        existing = self.get_task(task_id)
        if existing is None:
            raise ValueError(f"unknown background task: {task_id}")
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET status = 'running', updated_at = ?, started_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to start background task {task_id}")
        return updated

    def mark_completed(self, task_id: str, *, result_preview: str) -> BackgroundTaskRecord:
        return self.set_status(
            task_id,
            "completed",
            last_result_preview=result_preview,
            finished=True,
        )

    def request_stop(self, task_id: str) -> BackgroundTaskRecord:
        return self.set_status(task_id, "stopping", cancel_requested=True)

    def add_steer(self, task_id: str, message: str) -> BackgroundSteerRecord:
        if self.get_task(task_id) is None:
            raise ValueError(f"unknown background task: {task_id}")
        now = self.now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO cli_background_steers (task_id, message, status, created_at, consumed_at)
                VALUES (?, ?, 'pending', ?, NULL)
                """,
                (task_id, message, now),
            )
            steer_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET pending_steer_count = pending_steer_count + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
        return self.get_steer(steer_id)

    def get_steer(self, steer_id: int) -> BackgroundSteerRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, task_id, message, status, created_at, consumed_at
                FROM cli_background_steers
                WHERE id = ?
                """,
                (steer_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown background steer: {steer_id}")
        return self._steer_from_row(row)

    def consume_pending_steers(self, task_id: str) -> list[BackgroundSteerRecord]:
        now = self.now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, message, status, created_at, consumed_at
                FROM cli_background_steers
                WHERE task_id = ? AND status = 'pending'
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
            ids = [int(row[0]) for row in rows]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE cli_background_steers
                    SET status = 'consumed', consumed_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now, *ids],
                )
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET pending_steer_count = 0, updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
        return [self._steer_from_row(row) for row in rows]
