# Agent CLI Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add concurrent in-process background tasks, `/background`/`/tasks`/`/steer`/`/stop`/`/approve`, Boxed Console banner, and lightweight themes to `agent_cli`.

**Architecture:** Keep the ordinary terminal REPL and add a focused background registry in `agent_cli.background`. LangGraph checkpoints remain the conversation source of truth; new SQLite metadata tables only index CLI background tasks and steer messages. Background workers run in threads inside the current CLI process and reuse the existing agent runner with one LangGraph thread/session per background task.

**Tech Stack:** Python 3.12, argparse, prompt_toolkit, SQLite, threading, queue, LangGraph `Command(resume=...)`, pytest via `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## File Structure

- Create `agent_cli/background.py`
  - `BackgroundTaskRecord`, `BackgroundNotification`, `BackgroundTaskStore`, `BackgroundTaskRegistry`
  - task metadata schema and rendering helpers
  - worker loop, steer consumption, approval pause/resume, cooperative stop

- Create `agent_cli/banner.py`
  - `BannerContext`
  - `render_banner()`
  - wide Boxed Console and narrow fallback

- Create `agent_cli/theme.py`
  - `Theme`
  - `get_theme()`
  - `SUPPORTED_THEMES`
  - no prompt_toolkit skin engine; just lightweight terminal color helpers

- Modify `agent_cli/session_store.py`
  - initialize background task and steer schemas alongside `cli_sessions`
  - expose the DB path for `BackgroundTaskStore`

- Modify `agent_cli/commands.py`
  - add `/background`, `/tasks`, `/queue`, `/steer`, `/stop`, `/approve`
  - add `/agents` alias for `/tasks`

- Modify `agent_cli/repl.py`
  - accept a background registry and runtime display settings
  - route background commands
  - render banner at chat startup
  - drain background notifications in the main thread
  - stop active background work on exit

- Modify `agent_cli/config.py`
  - add `display_theme` to `RuntimeSettings`
  - validate `display.theme` as `default|mono|slate`

- Modify `agent_cli/main.py`
  - create/pass `BackgroundTaskStore` and `BackgroundTaskRegistry`
  - pass profile/home/theme metadata to `AgentCLI`

- Modify `agent_cli/input.py`
  - include new slash commands through command registry automatically

- Modify `agent_cli/doctor.py`
  - current semantic config validation will cover invalid `display.theme`

- Add tests:
  - `tests/test_agent_cli_background.py`
  - `tests/test_agent_cli_banner.py`
  - extend `tests/test_agent_cli_commands.py`
  - extend `tests/test_agent_cli_repl.py`
  - extend `tests/test_agent_cli_main.py`
  - extend `tests/test_agent_cli_doctor.py`

---

### Task 1: Background Metadata Store

**Files:**
- Create: `agent_cli/background.py`
- Modify: `agent_cli/session_store.py`
- Test: `tests/test_agent_cli_background.py`

- [ ] **Step 1: Write failing tests for task metadata and steer storage**

Create `tests/test_agent_cli_background.py` with:

```python
from pathlib import Path

from agent_cli.background import BackgroundTaskStore


def test_background_store_creates_and_lists_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    record = store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    assert record.task_id == "bg_12345678"
    assert record.session_id == "session-1"
    assert record.status == "queued"
    assert record.pending_steer_count == 0
    assert store.get_task("bg_12345678") == record
    assert [item.task_id for item in store.list_tasks()] == ["bg_12345678"]


def test_background_store_updates_status_result_and_error(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    store.mark_started("bg_12345678")
    running = store.get_task("bg_12345678")
    assert running.status == "running"
    assert running.started_at is not None

    store.mark_completed("bg_12345678", result_preview="All tests pass")
    completed = store.get_task("bg_12345678")
    assert completed.status == "completed"
    assert completed.last_result_preview == "All tests pass"
    assert completed.finished_at is not None

    store.set_status("bg_12345678", "failed", last_error="boom")
    failed = store.get_task("bg_12345678")
    assert failed.status == "failed"
    assert failed.last_error == "boom"


def test_background_store_queues_and_consumes_steer(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    first = store.add_steer("bg_12345678", "focus on pytest")
    second = store.add_steer("bg_12345678", "then update docs")

    assert first.id < second.id
    assert store.get_task("bg_12345678").pending_steer_count == 2

    pending = store.consume_pending_steers("bg_12345678")

    assert [item.message for item in pending] == ["focus on pytest", "then update docs"]
    assert store.get_task("bg_12345678").pending_steer_count == 0
    assert store.consume_pending_steers("bg_12345678") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing `BackgroundTaskStore`.

- [ ] **Step 3: Implement metadata dataclasses and schema**

Create `agent_cli/background.py` with:

```python
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
```

- [ ] **Step 4: Implement store methods**

Continue in `agent_cli/background.py`:

```python
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
```

- [ ] **Step 5: Verify background store can share the session DB path**

Add this regression assertion to `tests/test_agent_cli_background.py`:

```python
from agent_cli.session_store import SessionStore


def test_background_store_can_share_session_store_database(tmp_path: Path):
    session_store = SessionStore(tmp_path / "cli.sqlite")
    background_store = BackgroundTaskStore(session_store.db_path)

    record = background_store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    assert record.task_id == "bg_12345678"
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/background.py tests/test_agent_cli_background.py
git commit -m "feat: add cli background task store"
```

---

### Task 2: Background Registry Worker Loop

**Files:**
- Modify: `agent_cli/background.py`
- Test: `tests/test_agent_cli_background.py`

- [ ] **Step 1: Add failing tests for worker success, failure, and notifications**

Append to `tests/test_agent_cli_background.py`:

```python
from agent_cli.background import BackgroundTaskRegistry


def test_background_registry_runs_task_to_completion(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []
    sessions = []

    def runner(input_data, config):
        calls.append((input_data, config))
        return {"messages": [{"role": "assistant", "content": "done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        session_record_creator=lambda session_id, title: sessions.append((session_id, title)),
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("Fix tests")
    registry.join(record.task_id, timeout=2)

    completed = store.get_task(record.task_id)
    assert completed.status == "completed"
    assert completed.last_result_preview == "done"
    assert calls == [
        (
            {"messages": [{"role": "user", "content": "Fix tests"}]},
            {"configurable": {"thread_id": "session-1"}},
        )
    ]
    assert sessions == [("session-1", "Task title")]
    assert registry.drain_notifications()[0].kind == "done"


def test_background_registry_records_failure(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    def runner(input_data, config):
        raise RuntimeError("agent failed")

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("Fix tests")
    registry.join(record.task_id, timeout=2)

    failed = store.get_task(record.task_id)
    assert failed.status == "failed"
    assert "agent failed" in failed.last_error
    assert registry.drain_notifications()[0].kind == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py::test_background_registry_runs_task_to_completion tests/test_agent_cli_background.py::test_background_registry_records_failure -q
```

Expected: FAIL with missing `BackgroundTaskRegistry`.

- [ ] **Step 3: Implement registry primitives**

Append to `agent_cli/background.py`:

```python
import logging
import queue
import threading
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from agent_cli.interrupts import has_interrupt
from agent_cli.rendering import latest_ai_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundNotification:
    kind: str
    task_id: str
    session_id: str
    status: str
    message: str


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
```

- [ ] **Step 4: Implement task startup and join**

Append to `BackgroundTaskRegistry`:

```python
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
```

- [ ] **Step 5: Implement worker success/failure loop**

Append to `BackgroundTaskRegistry`:

```python
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
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py -q
```

Expected: PASS for store and worker tests.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/background.py tests/test_agent_cli_background.py
git commit -m "feat: add cli background task registry"
```

---

### Task 3: Steer, Stop, and Approval Resume

**Files:**
- Modify: `agent_cli/background.py`
- Test: `tests/test_agent_cli_background.py`

- [ ] **Step 1: Add failing tests for steer and stop**

Append to `tests/test_agent_cli_background.py`:

```python
def test_background_registry_consumes_queued_steer(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data["messages"][0]["content"])
        if len(calls) == 1:
            store.add_steer("bg_12345678", "second instruction")
        return {"messages": [{"role": "assistant", "content": f"turn {len(calls)}"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )
    registry.new_task_id = lambda: "bg_12345678"

    record = registry.start("first instruction")
    registry.join(record.task_id, timeout=2)

    assert calls == ["first instruction", "second instruction"]
    assert store.get_task(record.task_id).status == "completed"


def test_background_registry_stop_requests_interrupt_and_stops_after_turn(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    interrupts = []

    def runner(input_data, config):
        registry.stop("bg_12345678")
        return {"messages": [{"role": "assistant", "content": "done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
        stop_wait_interrupt=lambda session_id: interrupts.append(session_id),
    )
    registry.new_task_id = lambda: "bg_12345678"

    record = registry.start("first instruction")
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "stopped"
    assert interrupts == ["session-1"]
```

- [ ] **Step 2: Add failing test for approval pause/resume**

Append:

```python
def test_background_registry_waits_for_approval_and_resumes(tmp_path: Path):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data)
        if len(calls) == 1:
            return {
                "__interrupt__": [
                    {
                        "value": {
                            "action_requests": [{"name": "terminal", "args": {"command": "pwd"}}],
                            "review_configs": [{"description": "review"}],
                        }
                    }
                ]
            }
        assert isinstance(input_data, Command)
        assert input_data.resume == {"decisions": [{"type": "approve"}]}
        return {"messages": [{"role": "assistant", "content": "approved done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    assert store.get_task(record.task_id).status == "waiting_approval"

    approval_requests = registry.approval_requests(record.task_id)
    assert approval_requests[0].tool_name == "terminal"

    registry.approve(record.task_id, {"decisions": [{"type": "approve"}]})
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "completed"
    assert store.get_task(record.task_id).last_result_preview == "approved done"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py::test_background_registry_consumes_queued_steer tests/test_agent_cli_background.py::test_background_registry_stop_requests_interrupt_and_stops_after_turn tests/test_agent_cli_background.py::test_background_registry_waits_for_approval_and_resumes -q
```

Expected: FAIL with missing `stop`, `wait_for_status`, `approval_requests`, or `approve`.

- [ ] **Step 4: Implement stop and status wait**

Modify `BackgroundTaskRegistry` in `agent_cli/background.py`:

```python
    def stop(self, task_id: str) -> BackgroundTaskRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status in TERMINAL_TASK_STATUSES:
            return record
        if record.status == "stopping":
            return record
        updated = self.store.request_stop(task_id)
        self.stop_wait_interrupt(record.session_id)
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is not None:
                runtime.approval_event.set()
        return updated

    def wait_for_status(self, task_id: str, status: str, *, timeout: float) -> None:
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        while datetime.now(timezone.utc).timestamp() < deadline:
            record = self.store.get_task(task_id)
            if record is not None and record.status == status:
                return
            threading.Event().wait(0.01)
        record = self.store.get_task(task_id)
        raise AssertionError(f"task {task_id} did not reach {status}; current={record}")
```

- [ ] **Step 5: Implement approval inspection and resume**

Modify imports in `agent_cli/background.py`:

```python
from langgraph.types import Command

from agent_cli.interrupts import extract_interrupt_review_requests, has_interrupt
```

Append to `BackgroundTaskRegistry`:

```python
    def approval_requests(self, task_id: str):
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
            runtime.resume_value = resume_value
            runtime.approval_event.set()
        return record
```

- [ ] **Step 6: Update `_pause_for_approval` to resume the worker**

Replace `_pause_for_approval` in `agent_cli/background.py` with:

```python
    def _pause_for_approval(self, task_id: str, result: Any) -> None:
        waiting = self.store.set_status(task_id, "waiting_approval")
        with self._lock:
            runtime = self._runtime.get(task_id)
            if runtime is None:
                return
            runtime.interrupt_result = result
            runtime.resume_value = None
        self._notify(
            kind="attention",
            record=waiting,
            message=f"waiting approval; run /approve {task_id}",
        )

        while True:
            if self._stop_requested(task_id):
                stopped = self.store.set_status(task_id, "stopped", finished=True)
                self._notify(kind="stopped", record=stopped, message="stopped")
                return
            with self._lock:
                runtime = self._runtime.get(task_id)
                resume_value = runtime.resume_value if runtime is not None else None
            if resume_value is not None:
                break
            runtime.approval_event.wait(0.05)
            runtime.approval_event.clear()

        self.store.set_status(task_id, "running")
        resumed = self.runner(
            Command(resume=resume_value),
            {"configurable": {"thread_id": waiting.session_id}},
        )
        if has_interrupt(resumed):
            self._pause_for_approval(task_id, resumed)
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
                resumed = self._invoke(waiting.session_id, steer.message)
                if has_interrupt(resumed):
                    self._pause_for_approval(task_id, resumed)
                    return
        preview = latest_ai_text(resumed) or "completed"
        completed = self.store.mark_completed(task_id, result_preview=preview[:200])
        self._notify(kind="done", record=completed, message=preview[:200])
```

- [ ] **Step 7: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_cli/background.py tests/test_agent_cli_background.py
git commit -m "feat: support cli background steering and approval"
```

---

### Task 4: Command Registry and REPL Routing

**Files:**
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_commands.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add failing command registry tests**

Append to `tests/test_agent_cli_commands.py`:

```python
def test_phase_3_background_commands_are_registered():
    assert resolve_command("/background").name == "background"
    assert resolve_command("/tasks").name == "tasks"
    assert resolve_command("/agents").name == "tasks"
    assert resolve_command("/queue").name == "queue"
    assert resolve_command("/steer").name == "steer"
    assert resolve_command("/stop").name == "stop"
    assert resolve_command("/approve").name == "approve"

    help_text = render_help()
    assert "/background <prompt>" in help_text
    assert "/tasks" in help_text
    assert "/steer <task_id> <message>" in help_text
```

- [ ] **Step 2: Run command tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_phase_3_background_commands_are_registered -q
```

Expected: FAIL because commands are not registered.

- [ ] **Step 3: Register commands**

Modify `COMMAND_REGISTRY` in `agent_cli/commands.py` by inserting after `clear`:

```python
    CommandDef(
        "background",
        "Run a prompt as an in-process background task.",
        "Background",
        aliases=("bg",),
        args_hint="<prompt>",
    ),
    CommandDef("tasks", "List background tasks.", "Background", aliases=("agents",)),
    CommandDef("queue", "Show active background work.", "Background"),
    CommandDef(
        "steer",
        "Add a steering message to a background task.",
        "Background",
        args_hint="<task_id> <message>",
    ),
    CommandDef(
        "stop",
        "Cooperatively stop a background task.",
        "Background",
        args_hint="<task_id>",
    ),
    CommandDef(
        "approve",
        "Review a background task waiting for approval.",
        "Background",
        args_hint="<task_id>",
    ),
```

- [ ] **Step 4: Add failing REPL command tests**

Append to `tests/test_agent_cli_repl.py`:

```python
class FakeBackgroundRegistry:
    def __init__(self):
        self.started = []
        self.steers = []
        self.stopped = []
        self.approved = []
        self.notifications = []
        self.records = {}

    def start(self, prompt):
        record = SimpleNamespace(
            task_id="bg_12345678",
            session_id="session-bg",
            title="Background task",
            status="queued",
            pending_steer_count=0,
            last_result_preview=None,
            last_error=None,
            updated_at="now",
            created_at="now",
        )
        self.started.append(prompt)
        self.records[record.task_id] = record
        return record

    def list_tasks(self, active_only=False):
        return list(self.records.values())

    def steer(self, task_id, message):
        self.steers.append((task_id, message))
        return SimpleNamespace(id=1, task_id=task_id, message=message)

    def stop(self, task_id):
        self.stopped.append(task_id)
        return self.records[task_id]

    def approval_requests(self, task_id):
        from agent_cli.approval import ApprovalRequest

        return [ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {})]

    def approve(self, task_id, resume_value):
        self.approved.append((task_id, resume_value))

    def drain_notifications(self):
        items = self.notifications
        self.notifications = []
        return items


def test_handle_background_command_starts_task():
    registry = FakeBackgroundRegistry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command("/background fix tests")

    assert registry.started == ["fix tests"]
    assert "Started background task bg_12345678" in output
    assert "session-bg" in output


def test_handle_tasks_and_queue_render_background_tasks():
    registry = FakeBackgroundRegistry()
    registry.start("fix tests")
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    assert "bg_12345678" in cli.handle_command("/tasks")
    assert "bg_12345678" in cli.handle_command("/queue")


def test_handle_steer_stop_and_approve(monkeypatch):
    registry = FakeBackgroundRegistry()
    registry.start("fix tests")
    monkeypatch.setattr(
        "agent_cli.repl.collect_approval_decisions",
        lambda requests: {"decisions": [{"type": "approve"}]},
    )
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    assert "Queued steer" in cli.handle_command("/steer bg_12345678 retry now")
    assert "Stop requested" in cli.handle_command("/stop bg_12345678")
    assert "Approved background task" in cli.handle_command("/approve bg_12345678")

    assert registry.steers == [("bg_12345678", "retry now")]
    assert registry.stopped == ["bg_12345678"]
    assert registry.approved == [("bg_12345678", {"decisions": [{"type": "approve"}]})]
```

- [ ] **Step 5: Run REPL tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_handle_background_command_starts_task tests/test_agent_cli_repl.py::test_handle_tasks_and_queue_render_background_tasks tests/test_agent_cli_repl.py::test_handle_steer_stop_and_approve -q
```

Expected: FAIL because `AgentCLI` lacks `background_registry` routing.

- [ ] **Step 6: Add registry hooks and command routing**

Modify `AgentCLI.__init__` in `agent_cli/repl.py` to accept:

```python
        background_registry: Any | None = None,
        profile: str | None = None,
        cli_home: str | None = None,
        display_theme: str = "default",
        show_banner: bool = True,
```

Set attributes:

```python
        self.background_registry = background_registry
        self.profile = profile
        self.cli_home = cli_home
        self.display_theme = display_theme
        self.show_banner = show_banner
```

Add helpers to `AgentCLI`:

```python
    def _require_background_registry(self):
        if self.background_registry is None:
            raise RuntimeError("background tasks are not configured")
        return self.background_registry

    def _handle_background(self, arg: str) -> str:
        if not arg.strip():
            return "Usage: /background <prompt>"
        record = self._require_background_registry().start(arg)
        return f"Started background task {record.task_id} · session {record.session_id}"

    def _handle_tasks(self, *, active_only: bool = False) -> str:
        records = self._require_background_registry().list_tasks(active_only=active_only)
        if not records:
            return "No background tasks."
        lines = ["Background Tasks:"]
        for record in records:
            detail = record.last_error or record.last_result_preview or record.prompt_preview or ""
            lines.append(
                f"  {record.task_id}  {record.status:<16} {record.title} "
                f"session={record.session_id} steer={record.pending_steer_count} {detail}".rstrip()
            )
        return "\n".join(lines)

    def _handle_steer(self, arg: str) -> str:
        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            return "Usage: /steer <task_id> <message>"
        steer = self._require_background_registry().steer(parts[0], parts[1])
        return f"Queued steer {steer.id} for {steer.task_id}"

    def _handle_stop(self, arg: str) -> str:
        task_id = arg.strip()
        if not task_id:
            return "Usage: /stop <task_id>"
        record = self._require_background_registry().stop(task_id)
        if record.status in {"completed", "failed", "stopped"}:
            return f"Background task {record.task_id} is already {record.status}"
        return f"Stop requested for {record.task_id}"

    def _handle_approve(self, arg: str) -> str:
        task_id = arg.strip()
        if not task_id:
            return "Usage: /approve <task_id>"
        registry = self._require_background_registry()
        requests = registry.approval_requests(task_id)
        resume_value = collect_approval_decisions(requests)
        registry.approve(task_id, resume_value)
        return f"Approved background task {task_id}"
```

In `handle_command()`, before skills:

```python
        if command.name == "background":
            return self._handle_background(arg)
        if command.name == "tasks":
            return self._handle_tasks(active_only=False)
        if command.name == "queue":
            return self._handle_tasks(active_only=True)
        if command.name == "steer":
            return self._handle_steer(arg)
        if command.name == "stop":
            return self._handle_stop(arg)
        if command.name == "approve":
            return self._handle_approve(arg)
```

- [ ] **Step 7: Add registry convenience methods**

Append to `BackgroundTaskRegistry` in `agent_cli/background.py`:

```python
    def list_tasks(self, *, active_only: bool = False) -> list[BackgroundTaskRecord]:
        statuses = ACTIVE_TASK_STATUSES if active_only else None
        return self.store.list_tasks(statuses=statuses)

    def steer(self, task_id: str, message: str) -> BackgroundSteerRecord:
        record = self.store.get_task(task_id)
        if record is None:
            raise ValueError(f"unknown background task: {task_id}")
        if record.status in TERMINAL_TASK_STATUSES:
            raise ValueError(f"cannot steer background task in {record.status} state")
        return self.store.add_steer(task_id, message)
```

- [ ] **Step 8: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py tests/test_agent_cli_background.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add agent_cli/commands.py agent_cli/repl.py agent_cli/background.py tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py tests/test_agent_cli_background.py
git commit -m "feat: add cli background commands"
```

---

### Task 5: Notification Drain and REPL Exit Cleanup

**Files:**
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add failing tests for notification drain**

Append to `tests/test_agent_cli_repl.py`:

```python
def test_run_repl_drains_background_notifications_before_prompt(capsys):
    from agent_cli.background import BackgroundNotification

    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.notifications = [
                BackgroundNotification(
                    kind="done",
                    task_id="bg_12345678",
                    session_id="session-bg",
                    status="completed",
                    message="done",
                )
            ]

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=Registry(),
        show_banner=False,
    )

    assert cli.run_repl() == 0
    captured = capsys.readouterr()
    assert "[background done] bg_12345678" in captured.out


def test_run_repl_stops_active_background_tasks_on_exit(capsys):
    class Registry(FakeBackgroundRegistry):
        def list_tasks(self, active_only=False):
            return [
                SimpleNamespace(
                    task_id="bg_12345678",
                    session_id="session-bg",
                    title="Task",
                    status="running",
                    pending_steer_count=0,
                    last_result_preview=None,
                    last_error=None,
                    prompt_preview="Task",
                    updated_at="now",
                    created_at="now",
                )
            ]

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 0
    assert registry.stopped == ["bg_12345678"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_run_repl_drains_background_notifications_before_prompt tests/test_agent_cli_repl.py::test_run_repl_stops_active_background_tasks_on_exit -q
```

Expected: FAIL because notification drain and exit cleanup are not implemented.

- [ ] **Step 3: Implement notification formatting**

Add to `AgentCLI` in `agent_cli/repl.py`:

```python
    def _drain_background_notifications(self) -> None:
        if self.background_registry is None:
            return
        for item in self.background_registry.drain_notifications():
            print(
                f"[background {item.kind}] {item.task_id} · {item.status} "
                f"· session {item.session_id} · {item.message}"
            )

    def _stop_active_background_tasks_on_exit(self) -> None:
        if self.background_registry is None:
            return
        active = self.background_registry.list_tasks(active_only=True)
        if not active:
            return
        print(f"Stopping {len(active)} active background task(s)...")
        for record in active:
            try:
                self.background_registry.stop(record.task_id)
            except Exception as exc:
                print(f"Failed to stop {record.task_id}: {exc}", file=sys.stderr)
```

- [ ] **Step 4: Wire drain/cleanup into `run_repl()`**

Modify `run_repl()` in `agent_cli/repl.py`:

```python
    def run_repl(self) -> int:
        self.ensure_session()
        if self.show_banner:
            self._print_banner()
        else:
            print(f"Session: {self.session_id}")
            print("Type /help for commands. Ctrl-D exits.")
        while True:
            try:
                self._drain_background_notifications()
                text = self._prompt("> ").strip()
            except EOFError:
                print()
                self._stop_active_background_tasks_on_exit()
                return 0
            except KeyboardInterrupt:
                print()
                continue
            if not text:
                continue
            try:
                if text.startswith("/"):
                    output = self.handle_command(text)
                else:
                    output = self.submit_message(text)
                if output:
                    print(output)
                self._drain_background_notifications()
            except EOFError:
                self._stop_active_background_tasks_on_exit()
                return 0
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                continue
```

Add this first version of `_print_banner()` for this task:

```python
    def _print_banner(self) -> None:
        print(f"Session: {self.session_id}")
        print("Type /help for commands. Ctrl-D exits.")
```

Task 7 replaces it with Boxed Console rendering.

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "feat: surface cli background notifications"
```

---

### Task 6: Theme Config Validation

**Files:**
- Create: `agent_cli/theme.py`
- Modify: `agent_cli/config.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_doctor.py`

- [ ] **Step 1: Add failing theme unit tests**

Create `tests/test_agent_cli_banner.py` with initial theme tests:

```python
import pytest

from agent_cli.theme import get_theme


def test_get_theme_returns_supported_theme():
    theme = get_theme("default")

    assert theme.name == "default"
    assert theme.color_enabled is True


def test_get_theme_rejects_unknown_theme():
    with pytest.raises(ValueError, match="display.theme"):
        get_theme("weird")


def test_mono_theme_disables_color():
    assert get_theme("mono").color_enabled is False
```

- [ ] **Step 2: Add failing config validation test**

Append to `tests/test_agent_cli_main.py` or existing config-focused tests:

```python
def test_settings_from_config_reads_display_theme(tmp_path):
    from agent_cli.config import settings_from_config

    (tmp_path / "config.yaml").write_text("display:\n  theme: slate\n", encoding="utf-8")

    settings = settings_from_config(cli_home=tmp_path, profile=None, cli_model=None)

    assert settings.display_theme == "slate"


def test_settings_from_config_rejects_invalid_display_theme(tmp_path):
    from agent_cli.config import ConfigError, settings_from_config

    (tmp_path / "config.yaml").write_text("display:\n  theme: weird\n", encoding="utf-8")

    try:
        settings_from_config(cli_home=tmp_path, profile=None, cli_model=None)
    except ConfigError as exc:
        assert "display.theme" in str(exc)
    else:
        raise AssertionError("expected ConfigError")
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_banner.py tests/test_agent_cli_main.py::test_settings_from_config_reads_display_theme tests/test_agent_cli_main.py::test_settings_from_config_rejects_invalid_display_theme -q
```

Expected: FAIL due to missing `agent_cli.theme` and `RuntimeSettings.display_theme`.

- [ ] **Step 4: Implement `agent_cli/theme.py`**

Create `agent_cli/theme.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    color_enabled: bool
    border: str
    label: str
    success: str
    warning: str
    error: str
    reset: str = "\033[0m"

    def paint(self, text: str, color: str) -> str:
        if not self.color_enabled or not color:
            return text
        return f"{color}{text}{self.reset}"


SUPPORTED_THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        color_enabled=True,
        border="\033[38;5;67m",
        label="\033[38;5;111m",
        success="\033[38;5;71m",
        warning="\033[38;5;178m",
        error="\033[38;5;167m",
    ),
    "mono": Theme(
        name="mono",
        color_enabled=False,
        border="",
        label="",
        success="",
        warning="",
        error="",
    ),
    "slate": Theme(
        name="slate",
        color_enabled=True,
        border="\033[38;5;103m",
        label="\033[38;5;110m",
        success="\033[38;5;109m",
        warning="\033[38;5;179m",
        error="\033[38;5;168m",
    ),
}


def get_theme(name: str | None) -> Theme:
    key = name or "default"
    try:
        return SUPPORTED_THEMES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(SUPPORTED_THEMES))
        raise ValueError(f"display.theme must be one of: {allowed}") from exc
```

- [ ] **Step 5: Wire theme validation into config**

Modify `RuntimeSettings` in `agent_cli/config.py`:

```python
    display_theme: str = "default"
```

Add constant:

```python
DISPLAY_THEME_VALUES = {"default", "mono", "slate"}
```

In `settings_from_config()` after markdown validation:

```python
    display_theme = str(display_section.get("theme") or "default")
    if display_theme not in DISPLAY_THEME_VALUES:
        raise ConfigError(
            "display.theme must be one of: " + ", ".join(sorted(DISPLAY_THEME_VALUES))
        )
```

Return it:

```python
        display_theme=display_theme,
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_banner.py tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py -q
```

Expected: PASS, including existing doctor semantic config tests.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/theme.py agent_cli/config.py tests/test_agent_cli_banner.py tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py
git commit -m "feat: add cli display theme config"
```

---

### Task 7: Boxed Console Banner

**Files:**
- Create: `agent_cli/banner.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_banner.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add failing banner rendering tests**

Append to `tests/test_agent_cli_banner.py`:

```python
from agent_cli.banner import BannerContext, render_banner


def test_render_banner_uses_boxed_console_for_wide_terminal():
    context = BannerContext(
        workdir="/home/miku/projects/langchain",
        profile="dev",
        cli_home="/home/miku/.langchain-agent/profiles/dev",
        model_name="default",
        session_id="20260526_ab12cd34",
        background_counts={"running": 2, "waiting_approval": 1, "queued": 0, "completed": 5},
    )

    output = render_banner(context, width=100, theme_name="mono")

    assert "┌ Agent CLI" in output
    assert "profile  dev" in output
    assert "/background /tasks /steer /stop /approve" in output
    assert "RUN 2" in output
    assert "WAIT 1" in output


def test_render_banner_uses_narrow_fallback():
    context = BannerContext(
        workdir="/repo",
        profile=None,
        cli_home="/tmp/agent",
        model_name=None,
        session_id="s1",
        background_counts={},
    )

    output = render_banner(context, width=40, theme_name="mono")

    assert "Agent CLI" in output
    assert "session s1" in output
    assert "┌" not in output
```

- [ ] **Step 2: Run banner tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_banner.py::test_render_banner_uses_boxed_console_for_wide_terminal tests/test_agent_cli_banner.py::test_render_banner_uses_narrow_fallback -q
```

Expected: FAIL with missing `agent_cli.banner`.

- [ ] **Step 3: Implement banner rendering**

Create `agent_cli/banner.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from agent_cli.theme import get_theme


@dataclass(frozen=True)
class BannerContext:
    workdir: str
    profile: str | None
    cli_home: str | None
    model_name: str | None
    session_id: str
    background_counts: dict[str, int]


def _shorten(value: str | None, width: int) -> str:
    text = value or "-"
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return "..." + text[-(width - 3):]


def _line(label: str, value: str, inner_width: int) -> str:
    content = f" {label:<8} {value}"
    return "│" + content[:inner_width].ljust(inner_width) + "│"


def _section(title: str, inner_width: int) -> str:
    label = f" {title} "
    return "├" + label + "─" * max(0, inner_width - len(label)) + "┤"


def render_banner(context: BannerContext, *, width: int, theme_name: str = "default") -> str:
    theme = get_theme(theme_name)
    if width < 72:
        counts = context.background_counts
        return "\n".join(
            [
                "Agent CLI",
                f"cwd {context.workdir}",
                f"profile {context.profile or '-'}",
                f"home {context.cli_home or '-'}",
                f"model {context.model_name or 'default'}",
                f"session {context.session_id}",
                (
                    "background "
                    f"run={counts.get('running', 0)} "
                    f"wait={counts.get('waiting_approval', 0)} "
                    f"queued={counts.get('queued', 0)} "
                    f"done={counts.get('completed', 0)}"
                ),
            ]
        )

    box_width = min(width, 96)
    inner_width = box_width - 2
    top_label = " Agent CLI "
    top = "┌" + top_label + "─" * max(0, inner_width - len(top_label)) + "┐"
    bottom = "└" + "─" * inner_width + "┘"
    value_width = inner_width - 11
    counts = context.background_counts
    background = (
        f"RUN {counts.get('running', 0)}   "
        f"WAIT {counts.get('waiting_approval', 0)}   "
        f"QUEUED {counts.get('queued', 0)}   "
        f"DONE {counts.get('completed', 0)}"
    )
    lines = [
        top,
        _line("cwd", _shorten(context.workdir, value_width), inner_width),
        _line("profile", context.profile or "-", inner_width),
        _line("home", _shorten(context.cli_home, value_width), inner_width),
        _line("model", context.model_name or "default", inner_width),
        _line("session", context.session_id, inner_width),
        _section("Commands", inner_width),
        _line("", "/background /tasks /steer /stop /approve", inner_width),
        _line("", "/status /history /export /skills /doctor", inner_width),
        _section("Background", inner_width),
        _line("", background, inner_width),
        bottom,
    ]
    if theme_name == "mono":
        return "\n".join(lines)
    return "\n".join(theme.paint(line, theme.border) for line in lines)
```

- [ ] **Step 4: Add background summary helper**

Append to `BackgroundTaskRegistry` in `agent_cli/background.py`:

```python
    def summary_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in TASK_STATUSES}
        for record in self.store.list_tasks(limit=200):
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts
```

- [ ] **Step 5: Replace the first `_print_banner()` implementation**

Modify `_print_banner()` in `agent_cli/repl.py`:

```python
    def _print_banner(self) -> None:
        from shutil import get_terminal_size

        from agent_cli.banner import BannerContext, render_banner

        counts = (
            self.background_registry.summary_counts()
            if self.background_registry is not None
            else {}
        )
        context = BannerContext(
            workdir=self.workdir,
            profile=self.profile,
            cli_home=self.cli_home,
            model_name=self.model_name,
            session_id=self.session_id or "",
            background_counts=counts,
        )
        print(render_banner(context, width=get_terminal_size((100, 24)).columns, theme_name=self.display_theme))
        print("Type /help for commands. Ctrl-D exits.")
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_banner.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/banner.py agent_cli/background.py agent_cli/repl.py tests/test_agent_cli_banner.py tests/test_agent_cli_repl.py
git commit -m "feat: add cli boxed console banner"
```

---

### Task 8: Main Wiring, Checkpointer Handles, and Final Verification

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_doctor.py`
- Test: full Agent CLI suite

- [ ] **Step 1: Add failing main wiring test**

Append to `tests/test_agent_cli_main.py`:

```python
def test_make_cli_wires_background_registry(tmp_path):
    from types import SimpleNamespace

    from agent_cli.config import RuntimeSettings
    from agent_cli.main import make_cli
    from agent_cli.session_store import SessionStore

    args = SimpleNamespace(workdir=str(tmp_path), resume=None)
    store = SessionStore(tmp_path / "cli.sqlite")
    cli = make_cli(
        args=args,
        store=store,
        checkpointer=object(),
        settings=RuntimeSettings(
            profile="dev",
            cli_home=tmp_path,
            model_name="model",
            display_theme="slate",
        ),
    )

    assert cli.background_registry is not None
    assert cli.profile == "dev"
    assert cli.cli_home == str(tmp_path)
    assert cli.display_theme == "slate"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_make_cli_wires_background_registry -q
```

Expected: FAIL because `make_cli()` does not wire background registry/settings.

- [ ] **Step 3: Wire registry in `main.py`**

Modify imports in `agent_cli/main.py`:

```python
from agent_cli.background import BackgroundTaskRegistry, BackgroundTaskStore
from agent_core.terminal_lifecycle import interrupt_terminal_wait_for_thread_id
```

Modify `make_cli()`:

```python
def make_cli(
    *, args: argparse.Namespace, store: SessionStore, checkpointer, settings
) -> AgentCLI:
    workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
    background_store = BackgroundTaskStore(store.db_path)

    def run_background(input_data, config):
        handle = create_sqlite_checkpointer(store.db_path)
        try:
            agent = default_agent_factory(handle.checkpointer)
            return default_runner(agent, input_data, config)
        finally:
            handle.close()

    def create_background_session(session_id: str, title: str) -> None:
        store.create_session(
            workdir=workdir,
            model=settings.model_name,
            title=title,
            session_id=session_id,
        )

    background_registry = BackgroundTaskRegistry(
        store=background_store,
        session_id_factory=store.new_session_id,
        session_record_creator=create_background_session,
        title_factory=lambda prompt: store.title_from_message(prompt),
        runner=run_background,
        stop_wait_interrupt=lambda session_id: interrupt_terminal_wait_for_thread_id(session_id),
    )
    return AgentCLI(
        session_store=store,
        checkpointer=checkpointer,
        agent_factory=default_agent_factory,
        runner=default_runner,
        workdir=workdir,
        model_name=settings.model_name,
        default_title=settings.default_title,
        session_id=getattr(args, "resume", None),
        background_registry=background_registry,
        profile=settings.profile,
        cli_home=str(settings.cli_home) if settings.cli_home else None,
        display_theme=settings.display_theme,
        skill_commands_provider=lambda: load_skill_commands(
            built_in_names=set(COMMAND_LOOKUP)
        ),
        skill_discovery_provider=lambda: load_skill_discovery(
            built_in_names=set(COMMAND_LOOKUP)
        ),
    )
```

This plan always uses a fresh checkpointer handle for each background invocation
to avoid sharing SQLite connection state across worker threads.

- [ ] **Step 4: Run main/doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py -q
```

Expected: PASS.

- [ ] **Step 5: Run focused CLI suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_paths.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_input.py \
  tests/test_agent_cli_interrupts.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_doctor.py \
  tests/test_agent_cli_logging.py \
  tests/test_agent_cli_session.py \
  tests/test_agent_cli_session_store.py \
  tests/test_agent_cli_checkpoints.py \
  tests/test_agent_cli_builders.py \
  tests/test_agent_cli_background.py \
  tests/test_agent_cli_banner.py \
  -q
```

Expected: all listed tests PASS.

- [ ] **Step 6: Run smoke commands**

Run:

```bash
HOME="$(mktemp -d)" env -u AGENT_CLI_HOME /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --profile phase3 sessions
```

Expected output contains:

```text
No sessions found.
```

Run:

```bash
tmp_home="$(mktemp -d)"
printf 'display:\n  theme: weird\n' > "$tmp_home/config.yaml"
AGENT_CLI_HOME="$tmp_home" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor
```

Expected: doctor output contains a config failure for `display.theme` and exits non-zero.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/main.py agent_cli/repl.py tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py
git commit -m "feat: wire cli background runtime"
```

---

## Final Verification

- [ ] Run focused CLI suite:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_paths.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_input.py \
  tests/test_agent_cli_interrupts.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_doctor.py \
  tests/test_agent_cli_logging.py \
  tests/test_agent_cli_session.py \
  tests/test_agent_cli_session_store.py \
  tests/test_agent_cli_checkpoints.py \
  tests/test_agent_cli_builders.py \
  tests/test_agent_cli_background.py \
  tests/test_agent_cli_banner.py \
  -q
```

- [ ] Run adjacent terminal notification tests because Phase 3 uses the existing runner:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_runner.py \
  tests/test_terminal_notifications.py \
  tests/test_terminal_lifecycle.py \
  -q
```

- [ ] Check worktree:

```bash
git status --short
```

Expected: only intentionally untracked local files such as `.cursor/` or `.superpowers/`, no unstaged implementation changes.
