from __future__ import annotations

import logging
import threading
from typing import Any

from agent_core.session_context import hermes_task_id_from_runtime, hermes_task_id_from_thread_id
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry
from agent_tools.hermes_terminal_toolkit.terminal_tool import cleanup_vm

logger = logging.getLogger(__name__)
_recovery_attempted = False
_recovery_lock = threading.Lock()


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
