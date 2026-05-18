from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any

from agent_core.session_context import hermes_task_id_from_runtime, hermes_task_id_from_thread_id
from agent_tools.hermes_terminal_toolkit.interrupt import set_interrupt
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry
from agent_tools.hermes_terminal_toolkit.terminal_tool import cleanup_vm

logger = logging.getLogger(__name__)
_recovery_attempted = False
_recovery_lock = threading.Lock()
_active_execution_threads: dict[str, set[int]] = {}
_active_execution_lock = threading.Lock()


def recover_terminal_processes() -> int:
    """Recover host-backed Hermes background processes from checkpoint metadata once per process.

    Recovery is marked attempted before the registry call so failures do not retry in
    the same process. Concurrent callers wait for the first recovery attempt to finish
    before returning 0.
    """
    global _recovery_attempted
    with _recovery_lock:
        if _recovery_attempted:
            logger.info("Hermes terminal process recovery already attempted; skipping.")
            return 0
        _recovery_attempted = True
        recovered = process_registry.recover_from_checkpoint()
    logger.info("Recovered %s Hermes terminal process(es) from checkpoint.", recovered)
    return recovered


def cleanup_terminal_session_for_thread_id(thread_id: str | None) -> dict:
    """Explicitly end a terminal session: kill scoped processes and clean environment."""
    task_id = hermes_task_id_from_thread_id(thread_id)
    killed = process_registry.kill_all(task_id=task_id)
    cleanup_vm(task_id)
    return {
        "task_id": task_id,
        "killed_processes": killed,
        "environment_cleaned": True,
    }


def cleanup_terminal_session_for_runtime(runtime: Any | None) -> dict:
    """Explicitly end the terminal session associated with a ToolRuntime-like object."""
    task_id = hermes_task_id_from_runtime(runtime)
    killed = process_registry.kill_all(task_id=task_id)
    cleanup_vm(task_id)
    return {
        "task_id": task_id,
        "killed_processes": killed,
        "environment_cleaned": True,
    }


def end_terminal_session(thread_id: str | None, *, reason: str = "session_closed") -> dict:
    """Automatic session-end hook for callers that know the LangGraph thread id."""
    if not thread_id:
        return {
            "cleaned": False,
            "cleanup_reason": reason,
            "error": "thread_id is required for automatic terminal session cleanup",
        }
    result = cleanup_terminal_session_for_thread_id(thread_id)
    return {
        **result,
        "cleaned": True,
        "cleanup_reason": reason,
    }


@contextmanager
def terminal_execution_scope(thread_id: str | None):
    """Register the current Python thread as the active execution for a LangGraph thread."""
    if not thread_id:
        yield
        return

    key = str(thread_id)
    python_thread_id = threading.current_thread().ident
    if python_thread_id is None:
        yield
        return

    with _active_execution_lock:
        _active_execution_threads.setdefault(key, set()).add(python_thread_id)
        set_interrupt(False, thread_id=python_thread_id)

    try:
        yield
    finally:
        with _active_execution_lock:
            registered = _active_execution_threads.get(key)
            if registered is not None:
                registered.discard(python_thread_id)
                if not registered:
                    del _active_execution_threads[key]
            set_interrupt(False, thread_id=python_thread_id)


def interrupt_terminal_wait_for_thread_id(
    thread_id: str | None, *, reason: str = "new_user_message"
) -> dict:
    """Signal a blocking terminal/process wait for this LangGraph thread to return early."""
    if not thread_id:
        return {"interrupted": False, "reason": reason, "error": "thread_id is required"}

    with _active_execution_lock:
        python_thread_ids = set(_active_execution_threads.get(str(thread_id), set()))
    if not python_thread_ids:
        return {"interrupted": False, "reason": reason}

    for python_thread_id in python_thread_ids:
        set_interrupt(True, thread_id=python_thread_id)
    return {"interrupted": True, "reason": reason, "python_thread_ids": sorted(python_thread_ids)}
