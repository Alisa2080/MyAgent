from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


logger = logging.getLogger(__name__)

PROGRESS_OBSERVER_CONFIG_KEY = "progress_observer"
STREAMED_OUTPUT_MARKER = "__agent_streamed_output__"


def _freeze_progress_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_progress_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_progress_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_progress_value(item) for item in value)
    return value


@runtime_checkable
class ProgressObserver(Protocol):
    def emit(self, event: "ProgressEvent") -> None:
        ...


@dataclass(frozen=True)
class ModelStartEvent:
    thread_id: str | None = None
    kind: Literal["model_start"] = "model_start"


@dataclass(frozen=True)
class TokenDeltaEvent:
    text: str
    thread_id: str | None = None
    kind: Literal["token_delta"] = "token_delta"


@dataclass(frozen=True)
class ToolStartEvent:
    tool_name: str
    args: Mapping[str, Any]
    tool_call_id: str = ""
    thread_id: str | None = None
    kind: Literal["tool_start"] = "tool_start"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze_progress_value(self.args))


@dataclass(frozen=True)
class ToolCompleteEvent:
    tool_name: str
    args: Mapping[str, Any]
    result: Any
    duration_ms: int
    tool_call_id: str = ""
    thread_id: str | None = None
    blocked: bool = False
    kind: Literal["tool_complete"] = "tool_complete"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze_progress_value(self.args))


@dataclass(frozen=True)
class ToolErrorEvent:
    tool_name: str
    args: Mapping[str, Any]
    result: Any
    duration_ms: int
    error_message: str
    tool_call_id: str = ""
    thread_id: str | None = None
    kind: Literal["tool_error"] = "tool_error"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze_progress_value(self.args))


@dataclass(frozen=True)
class TurnCompleteEvent:
    thread_id: str | None = None
    streamed_output: bool = False
    kind: Literal["turn_complete"] = "turn_complete"


@dataclass(frozen=True)
class FallbackEvent:
    reason: str
    thread_id: str | None = None
    kind: Literal["fallback"] = "fallback"


@dataclass(frozen=True)
class ProgressHiddenEvent:
    hidden_count: int
    thread_id: str | None = None
    kind: Literal["progress_hidden"] = "progress_hidden"


ProgressEvent = (
    ModelStartEvent
    | TokenDeltaEvent
    | ToolStartEvent
    | ToolCompleteEvent
    | ToolErrorEvent
    | TurnCompleteEvent
    | FallbackEvent
    | ProgressHiddenEvent
)


def emit_progress(observer: ProgressObserver | None, event: ProgressEvent) -> None:
    if observer is None:
        return
    try:
        observer.emit(event)
    except Exception:
        logger.debug("Progress observer failed while handling %s", event, exc_info=True)


def set_progress_observer(
    config: dict[str, Any] | None,
    observer: ProgressObserver | None,
) -> dict[str, Any] | None:
    if observer is None:
        return config
    updated = dict(config or {})
    configurable = dict(updated.get("configurable") or {})
    configurable[PROGRESS_OBSERVER_CONFIG_KEY] = observer
    updated["configurable"] = configurable
    return updated


def get_progress_observer_from_config(config: dict[str, Any] | None) -> ProgressObserver | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    observer = configurable.get(PROGRESS_OBSERVER_CONFIG_KEY)
    if observer is None:
        return None
    if hasattr(observer, "emit"):
        return observer
    return None


def get_progress_observer_from_runtime(runtime: Any | None) -> ProgressObserver | None:
    return get_progress_observer_from_config(getattr(runtime, "config", None))


def with_thread(event: ProgressEvent, thread_id: str | None) -> ProgressEvent:
    if thread_id is None or getattr(event, "thread_id", None):
        return event
    return replace(event, thread_id=thread_id)


def mark_streamed_output(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        updated = dict(result)
        updated[STREAMED_OUTPUT_MARKER] = True
        return updated
    return {"final_response": "" if result is None else str(result), STREAMED_OUTPUT_MARKER: True}


def has_streamed_output(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get(STREAMED_OUTPUT_MARKER))
