from __future__ import annotations

import logging
import os
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

from agent_cli.langgraph_compat import Command
from agent_cli.interrupts import extract_interrupt_review_requests, has_interrupt
from agent_cli.rendering import latest_ai_text


TASK_STATUSES = {
    "queued",
    "running",
    "waiting_approval",
    "completing",
    "stopping",
    "stopped",
    "completed",
    "failed",
}
ACTIVE_TASK_STATUSES = {"queued", "running", "waiting_approval", "completing", "stopping"}
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


OWNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_background_owners (
  owner_id TEXT PRIMARY KEY,
  process_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
_NO_RESUME_VALUE = object()


@dataclass
class _RuntimeTask:
    thread: threading.Thread
    approval_event: threading.Event
    resume_value: Any = _NO_RESUME_VALUE
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
        reconcile_stale_tasks: bool = True,
        owner_id: str | None = None,
    ):
        self.store = store
        self.session_id_factory = session_id_factory
        self.session_record_creator = session_record_creator or (lambda session_id, title: None)
        self.title_factory = title_factory
        self.runner = runner
        self.stop_wait_interrupt = stop_wait_interrupt or (lambda session_id: None)
        self.owner_id = owner_id or f"owner_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._runtime: dict[str, _RuntimeTask] = {}
        self._notifications: queue.Queue[BackgroundNotification] = queue.Queue()
        self.store.register_owner(self.owner_id, process_id=os.getpid())
        if reconcile_stale_tasks:
            self.store.mark_stale_active_tasks_stopped(current_owner_id=self.owner_id)

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
            owner_id=self.owner_id,
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

    def list_tasks(
        self, *, active_only: bool = False, limit: int | None = 20
    ) -> list[BackgroundTaskRecord]:
        statuses = ACTIVE_TASK_STATUSES if active_only else None
        owner_id = self.owner_id if active_only else None
        return self.store.list_tasks(statuses=statuses, limit=limit, owner_id=owner_id)

    def steer(self, task_id: str, message: str) -> BackgroundSteerRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status in TERMINAL_TASK_STATUSES or record.status in {
            "completing",
            "stopping",
        }:
            raise ValueError(f"cannot steer background task in {record.status} state")
        return self.store.add_steer(task_id, message)

    def get_task(self, task_id: str) -> BackgroundTaskRecord | None:
        return self.store.get_task(task_id)

    def get_steers(self, task_id: str, *, limit: int = 20) -> list[BackgroundSteerRecord]:
        return self.store.get_steers(task_id, limit=limit)

    def join(self, task_id: str, *, timeout: float | None = None) -> None:
        with self._lock:
            runtime = self._runtime.get(task_id)
        if runtime is not None:
            runtime.thread.join(timeout)

    def stop(self, task_id: str) -> BackgroundTaskRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status in TERMINAL_TASK_STATUSES or record.status in {
            "completing",
            "stopping",
        }:
            return record

        updated = self.store.request_stop(task_id)
        self.stop_wait_interrupt(record.session_id)
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is not None:
                runtime.approval_event.set()
        return updated

    def finalize_stopping(self, task_id: str) -> BackgroundTaskRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status in TERMINAL_TASK_STATUSES or not record.cancel_requested:
            return record
        return self.store.mark_stopped(task_id)

    def wait_for_status(self, task_id: str, status: str, *, timeout: float) -> None:
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        while datetime.now(timezone.utc).timestamp() < deadline:
            record = self.store.get_task(task_id)
            if record is not None and record.status == status:
                return
            threading.Event().wait(0.01)
        record = self.store.get_task(task_id)
        raise AssertionError(f"task {task_id} did not reach {status}; current={record}")

    def approval_requests(self, task_id: str):
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status != "waiting_approval":
            raise ValueError(f"background task is not waiting for approval: {task_id}")
        with self._lock:
            runtime = self._runtime.get(task_id)
            result = runtime.interrupt_result if runtime is not None else None
        if result is None:
            raise ValueError(f"background task is not waiting for approval: {task_id}")
        return extract_interrupt_review_requests(result)

    def approve(self, task_id: str, resume_value: Any) -> BackgroundTaskRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status != "waiting_approval":
            raise ValueError(f"background task is not waiting for approval: {task_id}")
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is None:
                raise ValueError(f"background task runtime is unavailable: {task_id}")
            if runtime.resume_value is not _NO_RESUME_VALUE:
                raise ValueError(f"background task approval was already submitted: {task_id}")
            runtime.resume_value = resume_value
            runtime.approval_event.set()
        return record

    def drain_notifications(self) -> list[BackgroundNotification]:
        items: list[BackgroundNotification] = []
        while True:
            try:
                items.append(self._notifications.get_nowait())
            except queue.Empty:
                return items

    def summary_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in TASK_STATUSES}
        for record in self.store.list_tasks(limit=200):
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts

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
            started = self.store.mark_started(task_id)
            if started.status == "stopping" or started.cancel_requested:
                stopped = self.store.set_status(task_id, "stopped", finished=True)
                self._notify(kind="stopped", record=stopped, message="stopped")
                return
            if started.status in TERMINAL_TASK_STATUSES:
                return
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
                    record = self.store.mark_completing_if_idle(task_id)
                    if record is None:
                        continue
                    if record.status == "completing":
                        break
                    if record.status == "stopping" or record.cancel_requested:
                        stopped = self.store.set_status(task_id, "stopped", finished=True)
                        self._notify(kind="stopped", record=stopped, message="stopped")
                    return
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
            current = self.store.get_task(task_id)
            if current is not None and (
                current.status in TERMINAL_TASK_STATUSES
                or current.status == "stopping"
                or current.cancel_requested
            ):
                if current.status == "stopping" or current.cancel_requested:
                    stopped = self.store.set_status(task_id, "stopped", finished=True)
                    self._notify(kind="stopped", record=stopped, message="stopped")
                return
            failed = self.store.set_status(
                task_id,
                "failed",
                last_error=f"{type(exc).__name__}: {exc}",
                finished=True,
            )
            self._notify(kind="failed", record=failed, message=failed.last_error or "failed")
        finally:
            with self._lock:
                self._runtime.pop(task_id, None)

    def _stop_requested(self, task_id: str) -> bool:
        record = self.store.get_task(task_id)
        return bool(record and record.cancel_requested)

    def _pause_for_approval(self, task_id: str, result: Any) -> None:
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is None:
                return
            runtime.interrupt_result = result
            runtime.resume_value = _NO_RESUME_VALUE
        waiting = self.store.mark_waiting_approval(task_id)
        if waiting.status != "waiting_approval":
            if waiting.status == "stopped":
                self._notify(kind="stopped", record=waiting, message="stopped")
            return
        self._notify(
            kind="attention",
            record=waiting,
            message=f"waiting approval; run /approve {task_id}",
        )

        while True:
            if self._stop_requested(task_id):
                stopped = self.store.mark_stopped(task_id)
                self._notify(kind="stopped", record=stopped, message="stopped")
                return
            with self._lock:
                runtime = self._runtime.get(task_id)
                resume_value = (
                    runtime.resume_value if runtime is not None else _NO_RESUME_VALUE
                )
                approval_event = runtime.approval_event if runtime is not None else None
            if resume_value is not _NO_RESUME_VALUE:
                break
            if approval_event is not None:
                approval_event.wait(0.05)
                approval_event.clear()

        running = self.store.mark_running_after_approval(task_id)
        if running.status != "running":
            if running.status == "stopped":
                self._notify(kind="stopped", record=running, message="stopped")
            return
        resumed = self.runner(
            Command(resume=resume_value),
            {"configurable": {"thread_id": waiting.session_id}},
        )
        if self._stop_requested(task_id):
            stopped = self.store.set_status(task_id, "stopped", finished=True)
            self._notify(kind="stopped", record=stopped, message="stopped")
            return
        if has_interrupt(resumed):
            self._pause_for_approval(task_id, resumed)
            return
        while True:
            steers = self.store.consume_pending_steers(task_id)
            if not steers:
                record = self.store.mark_completing_if_idle(task_id)
                if record is None:
                    continue
                if record.status == "completing":
                    break
                if record.status == "stopping" or record.cancel_requested:
                    stopped = self.store.set_status(task_id, "stopped", finished=True)
                    self._notify(kind="stopped", record=stopped, message="stopped")
                return
            for steer in steers:
                if self._stop_requested(task_id):
                    stopped = self.store.set_status(task_id, "stopped", finished=True)
                    self._notify(kind="stopped", record=stopped, message="stopped")
                    return
                resumed = self._invoke(waiting.session_id, steer.message)
                if has_interrupt(resumed):
                    self._pause_for_approval(task_id, resumed)
                    return
        preview = latest_ai_text(resumed) or "completed"
        completed = self.store.mark_completed(task_id, result_preview=preview[:200])
        self._notify(kind="done", record=completed, message=preview[:200])


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
            conn.execute(OWNER_SCHEMA)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(cli_background_tasks)").fetchall()
            }
            if "owner_id" not in columns:
                conn.execute("ALTER TABLE cli_background_tasks ADD COLUMN owner_id TEXT")

    def register_owner(self, owner_id: str, *, process_id: int) -> None:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cli_background_owners (owner_id, process_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    process_id = excluded.process_id,
                    updated_at = excluded.updated_at
                """,
                (owner_id, process_id, now, now),
            )

    @staticmethod
    def _process_is_running(process_id: int) -> bool:
        if process_id <= 0:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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
        owner_id: str | None = None,
    ) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cli_background_tasks (
                    task_id, session_id, title, status, prompt_preview,
                    last_result_preview, last_error, created_at, updated_at,
                    started_at, finished_at, cancel_requested, pending_steer_count,
                    owner_id
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, NULL, 0, 0, ?)
                """,
                (task_id, session_id, title, prompt_preview, now, now, owner_id),
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
        limit: int | None = 20,
        owner_id: str | None = None,
    ) -> list[BackgroundTaskRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(statuses))
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
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
                {limit_clause}
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
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            existing = self._task_from_row(row)
            finished_at = now if finished else existing.finished_at
            cancel_value = int(existing.cancel_requested or bool(cancel_requested))
            result_preview = (
                last_result_preview
                if last_result_preview is not None
                else existing.last_result_preview
            )
            error_value = last_error if last_error is not None else existing.last_error
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

    def mark_stopped(self, task_id: str) -> BackgroundTaskRecord:
        return self.set_status(task_id, "stopped", cancel_requested=True, finished=True)

    def mark_waiting_approval(self, task_id: str) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            existing = self._task_from_row(row)
            if existing.status in TERMINAL_TASK_STATUSES:
                return existing
            if existing.status == "stopping" or existing.cancel_requested:
                conn.execute(
                    """
                    UPDATE cli_background_tasks
                    SET status = 'stopped', updated_at = ?, finished_at = ?,
                        cancel_requested = 1
                    WHERE task_id = ?
                    """,
                    (now, now, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE cli_background_tasks
                    SET status = 'waiting_approval', updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to mark background task waiting {task_id}")
        return updated

    def mark_running_after_approval(self, task_id: str) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            existing = self._task_from_row(row)
            if existing.status in TERMINAL_TASK_STATUSES:
                return existing
            if existing.status == "stopping" or existing.cancel_requested:
                conn.execute(
                    """
                    UPDATE cli_background_tasks
                    SET status = 'stopped', updated_at = ?, finished_at = ?,
                        cancel_requested = 1
                    WHERE task_id = ?
                    """,
                    (now, now, task_id),
                )
            elif existing.status == "waiting_approval":
                conn.execute(
                    """
                    UPDATE cli_background_tasks
                    SET status = 'running', updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to resume background task {task_id}")
        return updated

    def mark_stale_active_tasks_stopped(self, *, current_owner_id: str) -> None:
        now = self.now()
        placeholders = ", ".join("?" for _ in ACTIVE_TASK_STATUSES)
        live_owner_rows = []
        with self.connect() as conn:
            for owner_id, process_id in conn.execute(
                "SELECT owner_id, process_id FROM cli_background_owners"
            ).fetchall():
                if self._process_is_running(int(process_id)):
                    live_owner_rows.append(owner_id)
            if current_owner_id not in live_owner_rows:
                live_owner_rows.append(current_owner_id)
            live_placeholders = ", ".join("?" for _ in live_owner_rows)
            owner_clause = (
                "owner_id IS NULL"
                if not live_owner_rows
                else f"(owner_id IS NULL OR owner_id NOT IN ({live_placeholders}))"
            )
            conn.execute(
                f"""
                UPDATE cli_background_tasks
                SET status = 'stopped', updated_at = ?, finished_at = ?,
                    cancel_requested = 1,
                    last_error = COALESCE(last_error, ?)
                WHERE status IN ({placeholders}) AND {owner_clause}
                """,
                [
                    now,
                    now,
                    "stopped because the previous CLI process is no longer running",
                    *sorted(ACTIVE_TASK_STATUSES),
                    *live_owner_rows,
                ],
            )

    def mark_started(self, task_id: str) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            status = row[0]
            if status != "queued":
                record_row = conn.execute(
                    """
                    SELECT task_id, session_id, title, status, prompt_preview,
                           last_result_preview, last_error, created_at, updated_at,
                           started_at, finished_at, cancel_requested, pending_steer_count
                    FROM cli_background_tasks
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if record_row is None:
                    raise RuntimeError(f"failed to read background task {task_id}")
                return self._task_from_row(record_row)
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
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            status = row[0]
            if status in TERMINAL_TASK_STATUSES or status == "stopping":
                return self._task_from_row(
                    conn.execute(
                        """
                        SELECT task_id, session_id, title, status, prompt_preview,
                               last_result_preview, last_error, created_at, updated_at,
                               started_at, finished_at, cancel_requested, pending_steer_count
                        FROM cli_background_tasks
                        WHERE task_id = ?
                        """,
                        (task_id,),
                    ).fetchone()
                )
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET status = 'completed', updated_at = ?, finished_at = ?,
                    last_result_preview = ?
                WHERE task_id = ?
                """,
                (now, now, result_preview, task_id),
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to complete background task {task_id}")
        return updated

    def mark_completing_if_idle(self, task_id: str) -> BackgroundTaskRecord | None:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, pending_steer_count
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            status = row[0]
            pending_count = int(row[1])
            if pending_count:
                return None
            if status in TERMINAL_TASK_STATUSES or status == "stopping":
                record_row = conn.execute(
                    """
                    SELECT task_id, session_id, title, status, prompt_preview,
                           last_result_preview, last_error, created_at, updated_at,
                           started_at, finished_at, cancel_requested, pending_steer_count
                    FROM cli_background_tasks
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if record_row is None:
                    raise RuntimeError(f"failed to read background task {task_id}")
                return self._task_from_row(record_row)
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET status = 'completing', updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to mark background task completing {task_id}")
        return updated

    def request_stop(self, task_id: str) -> BackgroundTaskRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            status = row[0]
            if status in TERMINAL_TASK_STATUSES or status in {"completing", "stopping"}:
                record_row = conn.execute(
                    """
                    SELECT task_id, session_id, title, status, prompt_preview,
                           last_result_preview, last_error, created_at, updated_at,
                           started_at, finished_at, cancel_requested, pending_steer_count
                    FROM cli_background_tasks
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if record_row is None:
                    raise RuntimeError(f"failed to read background task {task_id}")
                return self._task_from_row(record_row)
            conn.execute(
                """
                UPDATE cli_background_tasks
                SET status = 'stopping', updated_at = ?, cancel_requested = 1
                WHERE task_id = ?
                """,
                (now, task_id),
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"failed to request stop for background task {task_id}")
        return updated

    def add_steer(self, task_id: str, message: str) -> BackgroundSteerRecord:
        now = self.now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status
                FROM cli_background_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown background task: {task_id}")
            status = row[0]
            if status in TERMINAL_TASK_STATUSES or status in {"completing", "stopping"}:
                raise ValueError(f"cannot steer background task in {status} state")
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
            conn.execute("BEGIN IMMEDIATE")
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
                SET pending_steer_count = (
                    SELECT COUNT(*)
                    FROM cli_background_steers
                    WHERE task_id = ? AND status = 'pending'
                ),
                    updated_at = ?
                WHERE task_id = ?
                """,
                (task_id, now, task_id),
            )
        return [self._steer_from_row(row) for row in rows]

    def get_steers(self, task_id: str, *, limit: int = 20) -> list[BackgroundSteerRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, message, status, created_at, consumed_at
                FROM cli_background_steers
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [self._steer_from_row(row) for row in reversed(rows)]
