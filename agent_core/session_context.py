from __future__ import annotations

from hashlib import sha256
from typing import Any


_TASK_ID_PREFIX = "lg_"
_TASK_ID_HASH_CHARS = 24
_FALLBACK_TASK_ID = "default"


def hermes_task_id_from_thread_id(thread_id: str | None) -> str:
    """Return a path-safe Hermes task id derived from a LangGraph thread id."""
    if not thread_id:
        return _FALLBACK_TASK_ID
    digest = sha256(str(thread_id).encode("utf-8")).hexdigest()[:_TASK_ID_HASH_CHARS]
    return f"{_TASK_ID_PREFIX}{digest}"


def hermes_task_id_from_runtime(runtime: Any | None) -> str:
    """Extract LangGraph thread identity from ToolRuntime-like objects."""
    thread_id = _thread_id_from_execution_info(runtime)
    if thread_id:
        return hermes_task_id_from_thread_id(thread_id)

    thread_id = _thread_id_from_config(runtime)
    return hermes_task_id_from_thread_id(thread_id)


def _thread_id_from_execution_info(runtime: Any | None) -> str | None:
    execution_info = getattr(runtime, "execution_info", None)
    if execution_info is None:
        return None
    value = getattr(execution_info, "thread_id", None)
    return str(value) if value else None


def _thread_id_from_config(runtime: Any | None) -> str | None:
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("thread_id")
    return str(value) if value else None
