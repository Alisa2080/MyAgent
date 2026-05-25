from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal


_TASK_ID_PREFIX = "lg_"
_TASK_ID_HASH_CHARS = 24
_FALLBACK_TASK_ID = "default"

ThreadSource = Literal["execution_info", "config", "fallback"]


@dataclass(frozen=True)
class RuntimeContext:
    """Resolved LangGraph/LangChain runtime identity for project tools."""

    thread_id: str | None
    task_id: str
    tool_call_id: str | None = None
    thread_source: ThreadSource = "fallback"

    @classmethod
    def from_runtime(cls, runtime: Any | None) -> "RuntimeContext":
        thread_id = _thread_id_from_execution_info(runtime)
        if thread_id:
            return cls(
                thread_id=thread_id,
                task_id=hermes_task_id_from_thread_id(thread_id),
                tool_call_id=_tool_call_id_from_runtime(runtime),
                thread_source="execution_info",
            )

        thread_id = _thread_id_from_config(runtime)
        if thread_id:
            return cls(
                thread_id=thread_id,
                task_id=hermes_task_id_from_thread_id(thread_id),
                tool_call_id=_tool_call_id_from_runtime(runtime),
                thread_source="config",
            )

        return cls(
            thread_id=None,
            task_id=_FALLBACK_TASK_ID,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            thread_source="fallback",
        )

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "RuntimeContext":
        thread_id = _thread_id_from_config_value(config)
        if thread_id:
            return cls(
                thread_id=thread_id,
                task_id=hermes_task_id_from_thread_id(thread_id),
                tool_call_id=None,
                thread_source="config",
            )

        return cls(
            thread_id=None,
            task_id=_FALLBACK_TASK_ID,
            tool_call_id=None,
            thread_source="fallback",
        )

    @classmethod
    def from_thread_id(cls, thread_id: str | None) -> "RuntimeContext":
        normalized = str(thread_id) if thread_id else None
        return cls(
            thread_id=normalized,
            task_id=hermes_task_id_from_thread_id(normalized),
            thread_source="fallback",
        )

    @property
    def has_thread(self) -> bool:
        return self.thread_id is not None

    @property
    def is_default_task(self) -> bool:
        return self.task_id == _FALLBACK_TASK_ID


def hermes_task_id_from_thread_id(thread_id: str | None) -> str:
    """Return a path-safe Hermes task id derived from a LangGraph thread id."""
    if not thread_id:
        return _FALLBACK_TASK_ID
    digest = sha256(str(thread_id).encode("utf-8")).hexdigest()[:_TASK_ID_HASH_CHARS]
    return f"{_TASK_ID_PREFIX}{digest}"


def hermes_task_id_from_runtime(runtime: Any | None) -> str:
    """Extract LangGraph thread identity from ToolRuntime-like objects."""
    return RuntimeContext.from_runtime(runtime).task_id


def _thread_id_from_execution_info(runtime: Any | None) -> str | None:
    execution_info = getattr(runtime, "execution_info", None)
    if execution_info is None:
        return None
    value = getattr(execution_info, "thread_id", None)
    return str(value) if value else None


def _thread_id_from_config(runtime: Any | None) -> str | None:
    config = getattr(runtime, "config", None)
    return _thread_id_from_config_value(config)


def _thread_id_from_config_value(config: Any | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("thread_id")
    return str(value) if value else None


def _tool_call_id_from_runtime(runtime: Any | None) -> str | None:
    try:
        value = getattr(runtime, "tool_call_id", None)
    except Exception:
        return None
    return str(value) if value else None
