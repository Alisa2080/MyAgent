from __future__ import annotations

from langchain_core.messages import ToolMessage

from agent_core.session_context import RuntimeContext


def _nested_tool_runtime(runtime: object, *, tool_call_id: str):
    if not tool_call_id:
        return runtime

    class _RuntimeProxy:
        def __init__(self, base_runtime: object, nested_tool_call_id: str) -> None:
            self._base_runtime = base_runtime
            self.tool_call_id = nested_tool_call_id

        def __getattr__(self, name: str):
            return getattr(self._base_runtime, name)

    return _RuntimeProxy(runtime, nested_tool_call_id=tool_call_id)


def normalize_rpc_args(tool_name: str, args: dict) -> dict:
    normalized = dict(args or {})
    if tool_name == "terminal":
        return {
            "command": str(normalized.get("command") or ""),
            "background": False,
            "timeout": normalized.get("timeout"),
            "workdir": normalized.get("workdir"),
            "pty": False,
            "notify_on_complete": False,
            "watch_patterns": None,
        }
    return normalized


def failure_payload(tool_name: str, message: str, *, code: str, data=None, meta=None) -> dict:
    return {
        "ok": False,
        "tool": tool_name,
        "message": message,
        "data": data,
        "error": {"code": code, "message": message},
        "meta": meta or {},
    }


def tool_message_to_rpc_payload(tool_name: str, value: object) -> dict:
    if isinstance(value, ToolMessage) and isinstance(value.artifact, dict):
        artifact = dict(value.artifact)
        return {
            "ok": bool(artifact.get("ok", value.status != "error")),
            "tool": str(artifact.get("tool") or tool_name),
            "message": str(artifact.get("message") or value.content or ""),
            "data": artifact.get("data"),
            "error": artifact.get("error"),
            "meta": dict(artifact.get("meta") or {}),
        }
    if isinstance(value, dict):
        return {
            "ok": bool(value.get("ok", True)),
            "tool": str(value.get("tool") or tool_name),
            "message": str(value.get("message") or ""),
            "data": value.get("data"),
            "error": value.get("error"),
            "meta": dict(value.get("meta") or {}),
        }
    return failure_payload(
        tool_name,
        "Tool returned a non-standard response.",
        code="invalid_response",
        data={"value": str(value)},
    )


class CodeExecutionDispatcher:
    def __init__(self, *, runtime: object, visible_tools: tuple[str, ...], max_tool_calls: int = 50) -> None:
        self.runtime = runtime
        self.visible_tools = set(visible_tools)
        self.max_tool_calls = int(max_tool_calls)
        self.tool_calls = 0
        runtime_context = RuntimeContext.from_runtime(runtime)
        self.outer_tool_call_id = str(runtime_context.tool_call_id or "")

    def dispatch(self, tool_name: str, args: dict) -> dict:
        if tool_name not in self.visible_tools:
            return failure_payload(tool_name, f"Tool is not available in execute_code: {tool_name}", code="tool_not_available")
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            return failure_payload(tool_name, "execute_code tool call limit exceeded.", code="tool_call_limit_exceeded")
        nested_tool_call_id = self._nested_tool_call_id(tool_name)
        try:
            result = self._call_tool(
                tool_name,
                normalize_rpc_args(tool_name, args),
                nested_tool_call_id=nested_tool_call_id,
            )
        except Exception as exc:
            return failure_payload(tool_name, f"RPC tool dispatch failed: {type(exc).__name__}: {exc}", code="dispatch_error")
        return tool_message_to_rpc_payload(tool_name, result)

    def _nested_tool_call_id(self, tool_name: str) -> str:
        prefix = self.outer_tool_call_id or "execute_code"
        return f"{prefix}:rpc:{self.tool_calls}:{tool_name}"

    def _call_tool(self, tool_name: str, args: dict, *, nested_tool_call_id: str) -> object:
        nested_runtime = _nested_tool_runtime(self.runtime, tool_call_id=nested_tool_call_id)
        if tool_name in {"read_file", "search_files", "write_file", "patch"}:
            from agent_tools.public import files
            tool_obj = getattr(files, tool_name)
            func = getattr(tool_obj, "func", tool_obj)
            return func(runtime=nested_runtime, **args)
        if tool_name == "terminal":
            from agent_tools.public.terminal import terminal
            func = getattr(terminal, "func", terminal)
            return func(runtime=nested_runtime, **args)
        if tool_name in {"web_search", "web_extract"}:
            from agent_tools.public import web
            tool_obj = getattr(web, tool_name)
            func = getattr(tool_obj, "func", tool_obj)
            return func(runtime=nested_runtime, **args)
        return failure_payload(tool_name, f"Tool is not implemented in execute_code: {tool_name}", code="tool_not_implemented")
