from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any

from agent_tools.hermes_terminal_toolkit.process_registry import process_registry

DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK = 3
MAX_BACKGROUND_PROCESSES_ENV = "HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK"
_quota_locks: dict[str, threading.Lock] = {}
_quota_locks_lock = threading.Lock()


def max_background_processes_per_task() -> int:
    raw = os.getenv(MAX_BACKGROUND_PROCESSES_ENV)
    try:
        value = int(raw) if raw is not None else DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    if value < 1:
        return DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    return value


def count_running_background_processes(task_id: str, registry: Any = process_registry) -> int:
    lock = getattr(registry, "_lock", None)
    running = getattr(registry, "_running", {})
    if lock is None:
        sessions = list(running.values())
    else:
        with lock:
            sessions = list(running.values())
    return sum(1 for session in sessions if session.task_id == task_id and not session.exited)


def background_quota_available(task_id: str, registry: Any = process_registry) -> tuple[bool, int, int]:
    current = count_running_background_processes(task_id, registry=registry)
    limit = max_background_processes_per_task()
    return current < limit, current, limit


@contextmanager
def background_quota_guard(task_id: str):
    """Serialize quota check plus background process creation for a single task id."""
    with _quota_locks_lock:
        lock = _quota_locks.setdefault(task_id, threading.Lock())
    with lock:
        yield
