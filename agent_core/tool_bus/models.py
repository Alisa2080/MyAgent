from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

try:
    from langgraph.types import Command
except ImportError:  # pragma: no cover - exercised by subprocess smoke tests without langgraph installed
    class Command:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

from agent_core.tool_catalog import ToolSpec


ToolResponse = ToolMessage | Command
PreToolHook = Callable[["ToolBusRequest"], ToolMessage | None]
PostToolHook = Callable[["ToolBusRequest", "ToolBusResult"], None]
TransformToolResultHook = Callable[["ToolBusRequest", ToolMessage], ToolMessage | None]


@dataclass(frozen=True)
class ToolBusRequest:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str
    runtime: Any
    request: ToolCallRequest
    spec: ToolSpec | None = None


@dataclass(frozen=True)
class ToolBusResult:
    result: ToolResponse
    duration_ms: int
    error: Exception | None = None


@dataclass
class ToolBusHooks:
    pre_tool_call: list[PreToolHook] = field(default_factory=list)
    post_tool_call: list[PostToolHook] = field(default_factory=list)
    transform_tool_result: list[TransformToolResultHook] = field(default_factory=list)
