from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_cli.interrupts import has_interrupt
from agent_cli.rendering import latest_ai_text


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


logger = logging.getLogger(__name__)


@dataclass
class _RuntimeTask:
    thread: threading.Thread
    approval_event: threading.Event
    resume_value: Any | None = None
    interrupt_result: Any | None = None


class BackgroundTaskRegistry:
    def __init__(
        self,
        *,
        store: BackgroundTaskStore,
        session_id_factory: Callable[[], str],
        session_record_creator: Callable[[str, str], None] | None = None,
        title_factory: Callable[[str], str],
        runner: Callable[[Any, dict[str, Any]], Any],
        stop_wait_interrupt: Callable[[str], Any] | None = None,
    ):
        self.store = store
        self.session_id_factory = session_id_factory
        self.session_record_creator = session_record_creator or (lambda session_id, title: None)
        self.title_factory = title_factory
        self.runner = runner
        self.stop_wait_interrupt = stop_wait_interrupt or (lambda session_id: None)
        self._lock = threading.RLock()
        self._runtime: dict[str, _RuntimeTask] = {}
        self._notifications: queue.Queue[BackgroundNotification] = queue.Queue()

    @staticmethod
    def new_task_id() -> str:
        return f"bg_{uuid.uuid4().hex[:8]}"

    def start(self, prompt: str) -> BackgroundTaskRecord:
        if not prompt.strip():
            raise ValueError("background prompt is required")
        task_id = self.new_task_id()
        session_id = self.session_id_factory()
        title = self.title_factory(prompt)
        self.session_record_creator(session_id, title)
        record = self.store.create_task(
            task_id=task_id,
            session_id=session_id,
            title=title,
            prompt_preview=prompt[:200],
        )
        approval_event = threading.Event()
        thread = threading.Thread(
            target=self._worker,
            args=(task_id, session_id, prompt),
            name=f"agent-cli-{task_id}",
            daemon=True,
        )
        with self._lock:
            self._runtime[task_id] = _RuntimeTask(
                thread=thread,
                approval_event=approval_event,
            )
        thread.start()
        return record

    def join(self, task_id: str, *, timeout: float | None = None) -> None:
        with self._lock:
            runtime = self._runtime.get(task_id)
        if runtime is not None:
            runtime.thread.join(timeout)

    def drain_notifications(self) -> list[BackgroundNotification]:
        items: list[BackgroundNotification] = []
        while True:
            try:
                items.append(self._notifications.get_nowait())
            except queue.Empty:
                return items

    def _notify(self, *, kind: str, record: BackgroundTaskRecord, message: str) -> None:
        self._notifications.put(
            BackgroundNotification(
                kind=kind,
                task_id=record.task_id,
                session_id=record.session_id,
                status=record.status,
                message=message,
            )
        )

    def _invoke(self, session_id: str, message: str) -> Any:
        return self.runner(
            {"messages": [{"role": "user", "content": message}]},
            {"configurable": {"thread_id": session_id}},
        )

    def _worker(self, task_id: str, session_id: str, prompt: str) -> None:
        try:
            self.store.mark_started(task_id)
            result = self._invoke(session_id, prompt)
            if has_interrupt(result):
                self._pause_for_approval(task_id, result)
                return
            if self._stop_requested(task_id):
                stopped = self.store.set_status(task_id, "stopped", finished=True)
                self._notify(kind="stopped", record=stopped, message="stopped")
                return
            while True:
                steers = self.store.consume_pending_steers(task_id)
                if not steers:
                    break
                for steer in steers:
                    if self._stop_requested(task_id):
                        stopped = self.store.set_status(task_id, "stopped", finished=True)
                        self._notify(kind="stopped", record=stopped, message="stopped")
                        return
                    result = self._invoke(session_id, steer.message)
                    if has_interrupt(result):
                        self._pause_for_approval(task_id, result)
                        return
            preview = latest_ai_text(result) or "completed"
            completed = self.store.mark_completed(task_id, result_preview=preview[:200])
            self._notify(kind="done", record=completed, message=preview[:200])
        except Exception as exc:
            logger.error("Background task %s failed:\n%s", task_id, traceback.format_exc())
            failed = self.store.set_status(
                task_id,
                "failed",
                last_error=f"{type(exc).__name__}: {exc}",
                finished=True,
            )
            self._notify(kind="failed", record=failed, message=failed.last_error or "failed")

    def _stop_requested(self, task_id: str) -> bool:
        record = self.store.get_task(task_id)
        return bool(record and record.cancel_requested)

    def _pause_for_approval(self, task_id: str, result: Any) -> None:
        waiting = self.store.set_status(task_id, "waiting_approval")
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is not None:
                runtime.interrupt_result = result
        self._notify(
            kind="attention",
            record=waiting,
            message=f"waiting approval; run /approve {task_id}",
        )


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
