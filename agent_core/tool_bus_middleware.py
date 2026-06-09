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

from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args
from agent_core.tool_catalog import ToolSpec
from agent_core.progress import (
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
    emit_progress,
    get_progress_observer_from_runtime,
)
from agent_core.session_context import RuntimeContext
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
        started = time.monotonic()
        try:
            bus_request, call_request = self._prepare_request(request)
            observer = get_progress_observer_from_runtime(bus_request.runtime)
            thread_id = RuntimeContext.from_runtime(bus_request.runtime).thread_id
            emit_progress(
                observer,
                ToolStartEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
            blocked = self._run_pre_hooks(bus_request)
            if blocked is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                return self._finalize_blocked(
                    bus_request,
                    blocked,
                    duration_ms,
                    observer=observer,
                    thread_id=thread_id,
                )

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
            return self._finalize(
                bus_request,
                result,
                duration_ms,
                error,
                observer=observer,
                thread_id=thread_id,
            )
        except ToolArgCoercionError as exc:
            logger.info("Tool %s received invalid input", _request_tool_name(request), exc_info=True)
            return _invalid_input_message(request, exc)
        except Exception as exc:
            logger.exception("Tool %s failed during ToolBusMiddleware request preparation", _request_tool_name(request))
            return _tool_exception_message(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolResponse]],
    ) -> ToolResponse:
        started = time.monotonic()
        try:
            bus_request, call_request = self._prepare_request(request)
            observer = get_progress_observer_from_runtime(bus_request.runtime)
            thread_id = RuntimeContext.from_runtime(bus_request.runtime).thread_id
            emit_progress(
                observer,
                ToolStartEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
            blocked = self._run_pre_hooks(bus_request)
            if blocked is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                return self._finalize_blocked(
                    bus_request,
                    blocked,
                    duration_ms,
                    observer=observer,
                    thread_id=thread_id,
                )

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
            return self._finalize(
                bus_request,
                result,
                duration_ms,
                error,
                observer=observer,
                thread_id=thread_id,
            )
        except ToolArgCoercionError as exc:
            logger.info("Tool %s received invalid input", _request_tool_name(request), exc_info=True)
            return _invalid_input_message(request, exc)
        except Exception as exc:
            logger.exception(
                "Tool %s failed during ToolBusMiddleware request preparation",
                _request_tool_name(request),
            )
            return _tool_exception_message(request, exc)

    def _prepare_request(self, request: ToolCallRequest) -> tuple[ToolBusRequest, ToolCallRequest]:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name") or "")
        raw_args = tool_call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {}
        call_request = request
        if self.coerce_args:
            coerced_args = normalize_tool_args(request.tool, args)
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

    def _run_post_hooks(self, bus_request: ToolBusRequest, bus_result: ToolBusResult) -> None:
        for hook in self.hooks.post_tool_call:
            try:
                hook(bus_request, bus_result)
            except Exception:
                logger.warning("ToolBus post hook failed", exc_info=True)

    def _finalize_blocked(
        self,
        bus_request: ToolBusRequest,
        result: ToolMessage,
        duration_ms: int,
        *,
        observer: Any = None,
        thread_id: str | None = None,
    ) -> ToolMessage:
        emit_progress(
            observer,
            ToolCompleteEvent(
                tool_name=bus_request.tool_name,
                args=bus_request.args,
                result=result,
                duration_ms=duration_ms,
                tool_call_id=bus_request.tool_call_id,
                thread_id=thread_id,
                blocked=True,
            ),
        )
        self._run_post_hooks(
            bus_request,
            ToolBusResult(result=result, duration_ms=duration_ms, error=None),
        )
        return result

    def _finalize(
        self,
        bus_request: ToolBusRequest,
        result: ToolResponse,
        duration_ms: int,
        error: Exception | None,
        *,
        observer: Any = None,
        thread_id: str | None = None,
    ) -> ToolResponse:
        bus_result = ToolBusResult(result=result, duration_ms=duration_ms, error=error)
        self._run_post_hooks(bus_request, bus_result)

        visible_result = result
        if isinstance(result, ToolMessage):
            transformed = self._run_transform_hooks(bus_request, result)
            visible_result = self._limit_result(bus_request.spec, transformed)

        if error is None:
            emit_progress(
                observer,
                ToolCompleteEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    result=visible_result,
                    duration_ms=duration_ms,
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
        else:
            emit_progress(
                observer,
                ToolErrorEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    result=visible_result,
                    duration_ms=duration_ms,
                    error_message=f"{type(error).__name__}: {error}",
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
        return visible_result

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
        serialized_artifact = _serialize_for_limit_check(artifact)
        if len(content) <= limit and len(serialized_artifact) <= limit:
            return result

        truncated_artifact, artifact_was_truncated = _truncate_artifact(artifact, limit)

        truncated_content = content
        if len(truncated_content) > limit:
            truncated_content = (
                truncated_content[:limit]
                + f"\n\n[truncated: original content was {len(content)} chars]"
            )
        if artifact_was_truncated:
            suffix = (
                f"[truncated: artifact exceeded {limit} chars"
                f"; original artifact was {len(serialized_artifact)} chars]"
            )
            truncated_content = f"{truncated_content}\n\n{suffix}" if truncated_content else suffix

        return ToolMessage(
            content=truncated_content,
            name=getattr(result, "name", spec.name),
            tool_call_id=getattr(result, "tool_call_id", ""),
            status=getattr(result, "status", "success"),
            artifact=truncated_artifact,
        )


def _serialize_for_limit_check(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _truncate_artifact(artifact: Any, limit: int) -> tuple[Any, bool]:
    serialized = _serialize_for_limit_check(artifact)
    if len(serialized) <= limit:
        return artifact, False
    return {
        "truncated": True,
        "original_chars": len(serialized),
        "preview": serialized[:limit],
    }, True


def _request_tool_name(request: ToolCallRequest) -> str:
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


def _request_tool_call_id(request: ToolCallRequest) -> str:
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


def _tool_exception_message(request: ToolCallRequest, exc: Exception) -> ToolMessage:
    tool_name = _request_tool_name(request)
    tool_call_id = _request_tool_call_id(request)
    runtime = _runtime_with_tool_call_id(getattr(request, "runtime", None), tool_call_id)
    return tool_failure(
        tool_name,
        f"Tool execution failed: {type(exc).__name__}: {exc}",
        code="tool_exception",
        runtime=runtime,
    )


def _invalid_input_message(request: ToolCallRequest, exc: ToolArgCoercionError) -> ToolMessage:
    tool_name = _request_tool_name(request)
    tool_call_id = _request_tool_call_id(request)
    runtime = _runtime_with_tool_call_id(getattr(request, "runtime", None), tool_call_id)
    return tool_failure(
        tool_name,
        str(exc),
        code="invalid_input",
        runtime=runtime,
    )


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
