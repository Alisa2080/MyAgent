from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

try:
    from langgraph.types import Command
except ImportError:  # pragma: no cover - exercised by subprocess smoke tests without langgraph installed
    class Command:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

from agent_core.tool_catalog import ToolSpec
from agent_tools.shared.tool_result import tool_failure


logger = logging.getLogger(__name__)

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


class ToolBusMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        hooks: ToolBusHooks | None = None,
        specs: Mapping[str, ToolSpec] | None = None,
        coerce_args: bool = True,
    ) -> None:
        super().__init__()
        self.hooks = hooks or ToolBusHooks()
        self.specs = dict(specs or {})
        self.coerce_args = coerce_args

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolResponse],
    ) -> ToolResponse:
        bus_request, call_request = self._prepare_request(request)
        blocked = self._run_pre_hooks(bus_request)
        if blocked is not None:
            return blocked

        started = time.monotonic()
        error = None
        try:
            result = handler(call_request)
        except Exception as exc:
            error = exc
            logger.exception("Tool %s failed in ToolBusMiddleware", bus_request.tool_name)
            result = tool_failure(
                bus_request.tool_name,
                f"Tool execution failed: {type(exc).__name__}: {exc}",
                code="tool_exception",
                runtime=_runtime_with_tool_call_id(request.runtime, bus_request.tool_call_id),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return self._finalize(bus_request, result, duration_ms, error)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolResponse]],
    ) -> ToolResponse:
        bus_request, call_request = self._prepare_request(request)
        blocked = self._run_pre_hooks(bus_request)
        if blocked is not None:
            return blocked

        started = time.monotonic()
        error = None
        try:
            result = await handler(call_request)
        except Exception as exc:
            error = exc
            logger.exception("Tool %s failed in ToolBusMiddleware", bus_request.tool_name)
            result = tool_failure(
                bus_request.tool_name,
                f"Tool execution failed: {type(exc).__name__}: {exc}",
                code="tool_exception",
                runtime=_runtime_with_tool_call_id(request.runtime, bus_request.tool_call_id),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return self._finalize(bus_request, result, duration_ms, error)

    def _prepare_request(self, request: ToolCallRequest) -> tuple[ToolBusRequest, ToolCallRequest]:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name") or "")
        raw_args = tool_call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {}
        call_request = request
        if self.coerce_args:
            coerced_args = _coerce_tool_args(request.tool, args)
            if coerced_args != args:
                tool_call = {**tool_call, "args": coerced_args}
                call_request = _override_request_tool_call(request, tool_call)
                args = coerced_args
        bus_request = ToolBusRequest(
            tool_name=tool_name,
            args=args,
            tool_call_id=str(tool_call.get("id") or ""),
            runtime=request.runtime,
            request=call_request,
            spec=self.specs.get(tool_name),
        )
        return bus_request, call_request

    def _run_pre_hooks(self, bus_request: ToolBusRequest) -> ToolMessage | None:
        for hook in self.hooks.pre_tool_call:
            try:
                result = hook(bus_request)
            except Exception:
                logger.warning("ToolBus pre hook failed", exc_info=True)
                continue
            if isinstance(result, ToolMessage):
                return result
        return None

    def _finalize(
        self,
        bus_request: ToolBusRequest,
        result: ToolResponse,
        duration_ms: int,
        error: Exception | None,
    ) -> ToolResponse:
        bus_result = ToolBusResult(result=result, duration_ms=duration_ms, error=error)
        for hook in self.hooks.post_tool_call:
            try:
                hook(bus_request, bus_result)
            except Exception:
                logger.warning("ToolBus post hook failed", exc_info=True)

        if not isinstance(result, ToolMessage):
            return result

        transformed = self._run_transform_hooks(bus_request, result)
        limited = self._limit_result(bus_request.spec, transformed)
        return limited

    def _run_transform_hooks(self, bus_request: ToolBusRequest, result: ToolMessage) -> ToolMessage:
        for hook in self.hooks.transform_tool_result:
            try:
                transformed = hook(bus_request, result)
            except Exception:
                logger.warning("ToolBus transform hook failed", exc_info=True)
                continue
            if isinstance(transformed, ToolMessage):
                return transformed
        return result

    def _limit_result(self, spec: ToolSpec | None, result: ToolMessage) -> ToolMessage:
        if spec is None or spec.max_result_size_chars is None:
            return result
        limit = int(spec.max_result_size_chars)
        if limit <= 0:
            return result
        content = str(getattr(result, "content", "") or "")
        artifact = getattr(result, "artifact", None)
        if len(content) <= limit and len(str(artifact)) <= limit:
            return result

        new_artifact = copy.deepcopy(artifact)
        if isinstance(new_artifact, dict):
            data = new_artifact.get("data")
            if data is not None and len(str(data)) > limit:
                new_artifact["data"] = {
                    "truncated": True,
                    "original_chars": len(str(data)),
                    "preview": str(data)[:limit],
                }

        truncated_content = content
        if len(truncated_content) > limit:
            truncated_content = (
                truncated_content[:limit]
                + f"\n\n[truncated: original content was {len(content)} chars]"
            )
        elif len(str(artifact)) > limit:
            truncated_content = (
                truncated_content
                + f"\n\n[truncated: artifact exceeded {limit} chars]"
            )

        return ToolMessage(
            content=truncated_content,
            name=getattr(result, "name", spec.name),
            tool_call_id=getattr(result, "tool_call_id", ""),
            status=getattr(result, "status", "success"),
            artifact=new_artifact,
        )


def _coerce_tool_args(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return args
    schema = _tool_arg_schema(tool)
    if not schema:
        return args
    coerced = dict(args)
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        expected = _expected_type(schema.get(key))
        if expected is None:
            continue
        coerced[key] = _coerce_value(value, expected)
    return coerced


def _tool_arg_schema(tool: Any) -> dict[str, Any]:
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return args
    args_schema = getattr(tool, "args_schema", None)
    model_fields = getattr(args_schema, "model_fields", None)
    if isinstance(model_fields, dict):
        schema = {}
        for name, field in model_fields.items():
            annotation = getattr(field, "annotation", None)
            if annotation is int:
                schema[name] = {"type": "integer"}
            elif annotation is float:
                schema[name] = {"type": "number"}
            elif annotation is bool:
                schema[name] = {"type": "boolean"}
            elif annotation is list:
                schema[name] = {"type": "array"}
            elif annotation is dict:
                schema[name] = {"type": "object"}
        return schema
    return {}


def _expected_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if item != "null":
                return str(item)
    return None


def _coerce_value(value: str, expected_type: str) -> Any:
    if expected_type == "integer":
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return value
        if parsed == int(parsed):
            return int(parsed)
        return value
    if expected_type == "number":
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return value
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return value
        return int(parsed) if parsed == int(parsed) else parsed
    if expected_type == "boolean":
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return value
    if expected_type == "array":
        return _coerce_json(value, list)
    if expected_type == "object":
        return _coerce_json(value, dict)
    return value


def _coerce_json(value: str, expected_python_type: type) -> Any:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, expected_python_type) else value


def _override_request_tool_call(request: ToolCallRequest, tool_call: dict[str, Any]) -> ToolCallRequest:
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


class _RuntimeToolCallProxy:
    def __init__(self, runtime: Any, tool_call_id: str) -> None:
        self._runtime = runtime
        self.tool_call_id = tool_call_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


def _runtime_with_tool_call_id(runtime: Any, tool_call_id: str) -> Any:
    if not tool_call_id:
        return runtime
    try:
        existing = getattr(runtime, "tool_call_id", None)
    except Exception:
        existing = None
    if existing:
        return runtime
    return _RuntimeToolCallProxy(runtime, tool_call_id)
