from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware import ToolCallRequest


def request_tool_name(request: ToolCallRequest) -> str:
    try:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
        if tool_name:
            return str(tool_name)
    except Exception:
        pass
    try:
        tool = getattr(request, "tool", None)
        tool_name = getattr(tool, "name", None)
        if tool_name:
            return str(tool_name)
    except Exception:
        pass
    return ""


def request_tool_call_id(request: ToolCallRequest) -> str:
    try:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        if tool_call_id:
            return str(tool_call_id)
    except Exception:
        pass
    try:
        runtime = getattr(request, "runtime", None)
        tool_call_id = getattr(runtime, "tool_call_id", None)
        if tool_call_id:
            return str(tool_call_id)
    except Exception:
        pass
    return ""


def request_tool_args(request: ToolCallRequest) -> dict[str, Any]:
    try:
        tool_call = getattr(request, "tool_call", None) or {}
        raw_args = tool_call.get("args") if isinstance(tool_call, dict) else None
        return raw_args if isinstance(raw_args, dict) else {}
    except Exception:
        return {}


def override_request_tool_call(request: ToolCallRequest, tool_call: dict[str, Any]) -> ToolCallRequest:
    override = getattr(request, "override", None)
    if callable(override):
        return override(tool_call=tool_call)

    try:
        copied = copy.copy(request)
        copied.tool_call = tool_call
        return copied
    except Exception:
        return SimpleNamespace(
            tool_call=tool_call,
            runtime=getattr(request, "runtime", None),
            tool=getattr(request, "tool", None),
            state=getattr(request, "state", {}),
        )


class RuntimeToolCallProxy:
    def __init__(self, runtime: Any, tool_call_id: str) -> None:
        self._runtime = runtime
        self.tool_call_id = tool_call_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


def runtime_with_tool_call_id(runtime: Any, tool_call_id: str) -> Any:
    if not tool_call_id:
        return runtime
    try:
        existing = getattr(runtime, "tool_call_id", None)
    except Exception:
        existing = None
    if existing:
        return runtime
    return RuntimeToolCallProxy(runtime, tool_call_id)
