from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from agent_core.tool_args.coercion import ToolArgCoercionError, normalize_tool_args
from agent_core.tool_catalog import ToolSpec
from agent_core.tool_bus.models import (
    PostToolHook,
    PreToolHook,
    ToolBusHooks,
    ToolBusRequest,
    ToolBusResult,
    ToolResponse,
    TransformToolResultHook,
)
from agent_core.tool_bus.request import (
    override_request_tool_call,
    request_tool_args,
    request_tool_call_id,
    request_tool_name,
    runtime_with_tool_call_id,
)
from agent_core.tool_bus.result_limits import (
    limit_tool_result,
    serialize_for_limit_check,
    truncate_artifact,
)
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
                    runtime=runtime_with_tool_call_id(request.runtime, bus_request.tool_call_id),
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
            logger.info("Tool %s received invalid input", request_tool_name(request), exc_info=True)
            result = _invalid_input_message(request, exc)
            duration_ms = int((time.monotonic() - started) * 1000)
            _emit_invalid_input_progress(request, result, duration_ms, exc)
            return result
        except Exception as exc:
            logger.exception("Tool %s failed during ToolBusMiddleware request preparation", request_tool_name(request))
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
                    runtime=runtime_with_tool_call_id(request.runtime, bus_request.tool_call_id),
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
            logger.info("Tool %s received invalid input", request_tool_name(request), exc_info=True)
            result = _invalid_input_message(request, exc)
            duration_ms = int((time.monotonic() - started) * 1000)
            _emit_invalid_input_progress(request, result, duration_ms, exc)
            return result
        except Exception as exc:
            logger.exception(
                "Tool %s failed during ToolBusMiddleware request preparation",
                request_tool_name(request),
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
                call_request = override_request_tool_call(request, tool_call)
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
            visible_result = limit_tool_result(bus_request.spec, transformed)

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

_serialize_for_limit_check = serialize_for_limit_check
_truncate_artifact = truncate_artifact
_request_tool_name = request_tool_name
_request_tool_call_id = request_tool_call_id
_request_tool_args = request_tool_args
_override_request_tool_call = override_request_tool_call
_runtime_with_tool_call_id = runtime_with_tool_call_id


def _emit_invalid_input_progress(
    request: ToolCallRequest,
    result: ToolMessage,
    duration_ms: int,
    exc: ToolArgCoercionError,
) -> None:
    runtime = getattr(request, "runtime", None)
    observer = get_progress_observer_from_runtime(runtime)
    thread_id = RuntimeContext.from_runtime(runtime).thread_id
    emit_progress(
        observer,
        ToolErrorEvent(
            tool_name=_request_tool_name(request),
            args=_request_tool_args(request),
            result=result,
            duration_ms=duration_ms,
            error_message=str(exc),
            tool_call_id=_request_tool_call_id(request),
            thread_id=thread_id,
        ),
    )


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
