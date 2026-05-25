import json
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from agent_core.session_context import RuntimeContext


SummaryBuilder = Callable[[dict[str, Any], dict[str, Any]], str]


def _tool_call_id(runtime: ToolRuntime | None) -> str:
    if runtime is None:
        return ""
    return RuntimeContext.from_runtime(runtime).tool_call_id or ""


def _content(message: str) -> str:
    return str(message or "").strip() or "Tool completed."


def _artifact(
    tool: str,
    *,
    ok: bool,
    message: str,
    data: Any = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "message": message,
        "data": data,
        "error": error,
        "meta": meta or {},
    }


def tool_success(
    tool: str,
    *,
    message: str,
    data: Any = None,
    meta: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
    content: str | None = None,
) -> ToolMessage:
    payload = artifact or _artifact(tool, ok=True, message=message, data=data, error=None, meta=meta)
    return ToolMessage(
        content=_content(content or message),
        name=tool,
        tool_call_id=_tool_call_id(runtime),
        status="success",
        artifact=payload,
    )


def tool_failure(
    tool: str,
    message: str,
    *,
    code: str = "tool_error",
    data: Any = None,
    meta: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
    content: str | None = None,
) -> ToolMessage:
    payload = _artifact(
        tool,
        ok=False,
        message=message,
        data=data,
        error={"code": code, "message": message},
        meta=meta,
    )
    return ToolMessage(
        content=_content(content or message),
        name=tool,
        tool_call_id=_tool_call_id(runtime),
        status="error",
        artifact=payload,
    )


def _extract_meta(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}

    warning = payload.pop("_warning", None)
    if warning:
        meta["warnings"] = warning if isinstance(warning, list) else [warning]

    hint = payload.pop("_hint", None)
    if hint:
        meta["hint"] = hint

    for key in keys:
        if key in payload:
            meta[key] = payload.pop(key)

    return meta


def from_legacy_json(
    tool: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
    runtime: ToolRuntime | None = None,
    summary: SummaryBuilder | None = None,
) -> ToolMessage:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return tool_failure(
            tool,
            "Tool returned invalid response.",
            code="invalid_response",
            data={"raw": raw},
            runtime=runtime,
        )

    if not isinstance(payload, dict):
        return tool_failure(
            tool,
            f"Tool returned unexpected response type: {type(payload).__name__}.",
            code="invalid_response",
            data={"raw": raw},
            runtime=runtime,
        )

    message = str(payload.pop("message", success_message) or success_message)
    payload.pop("status", None)
    payload.pop("success", None)
    meta = _extract_meta(payload, *meta_keys)
    error_message = payload.pop("error", None)

    if error_message:
        return tool_failure(
            tool,
            str(error_message),
            code="tool_error",
            data=payload or None,
            meta=meta,
            runtime=runtime,
        )

    content = summary(payload, meta) if summary is not None else message
    return tool_success(
        tool,
        message=message,
        data=payload or None,
        meta=meta,
        runtime=runtime,
        content=content,
    )
