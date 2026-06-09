from __future__ import annotations

import os
import re

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure

STAGE_ONE_ALLOWED_TOOLS = frozenset({
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "terminal",
})

WEB_ALLOWED_TOOLS = frozenset({"web_search", "web_extract"})
SANDBOX_ALLOWED_TOOLS = STAGE_ONE_ALLOWED_TOOLS | WEB_ALLOWED_TOOLS

TOOL_ORDER = (
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "terminal",
    "web_search",
    "web_extract",
)


def visible_sandbox_tools(
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    *,
    include_web: bool,
) -> tuple[str, ...]:
    allowed = set(STAGE_ONE_ALLOWED_TOOLS)
    if include_web:
        allowed.update(WEB_ALLOWED_TOOLS)
    enabled = set(enabled_tools or allowed)
    return tuple(name for name in TOOL_ORDER if name in allowed and name in enabled)


_TOOL_STUBS = {
    "read_file": (
        "read_file",
        "path, offset=1, limit=500",
        "Read a text file with line numbers and pagination.",
        '{"path": path, "offset": offset, "limit": limit}',
    ),
    "search_files": (
        "search_files",
        'pattern, path=".", target="content", file_glob=None, limit=50, offset=0, output_mode="content", context=0',
        "Search files by content or filename.",
        '{"pattern": pattern, "path": path, "target": target, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}',
    ),
    "write_file": (
        "write_file",
        "path, content",
        "Write complete content to a workspace file.",
        '{"path": path, "content": content}',
    ),
    "patch": (
        "patch",
        'mode="replace", path=None, old_string=None, new_string=None, replace_all=False, patch=None',
        "Apply a replace or V4A patch.",
        '{"mode": mode, "path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "patch": patch}',
    ),
    "terminal": (
        "terminal",
        "command, timeout=None, workdir=None",
        "Execute a foreground shell command. Background and PTY execution are not supported.",
        '{"command": command, "timeout": timeout, "workdir": workdir}',
    ),
    "web_search": (
        "web_search",
        "query, limit=5",
        "Search the web and return metadata results.",
        '{"query": query, "limit": limit}',
    ),
    "web_extract": (
        "web_extract",
        'urls, format="markdown", use_llm_processing=True, model=None, min_length=2000, max_chars_per_url=20000',
        "Extract content from one or more web URLs.",
        '{"urls": urls, "format": format, "use_llm_processing": use_llm_processing, "model": model, "min_length": min_length, "max_chars_per_url": max_chars_per_url}',
    ),
}

_UDS_TRANSPORT_HEADER = r'''
import json
import os
import socket

_RPC_SOCKET = os.environ.get("CODE_EXECUTION_RPC_SOCKET", "")

def _call(tool_name, args):
    if not _RPC_SOCKET:
        return {"ok": False, "tool": tool_name, "message": "RPC socket is not configured.", "error": {"code": "rpc_unavailable", "message": "RPC socket is not configured."}, "data": None, "meta": {}}
    payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(_RPC_SOCKET)
        sock.sendall(len(payload).to_bytes(8, "big") + payload)
        header = sock.recv(8)
        if len(header) != 8:
            return {"ok": False, "tool": tool_name, "message": "Invalid RPC response header.", "error": {"code": "rpc_protocol_error", "message": "Invalid RPC response header."}, "data": None, "meta": {}}
        size = int.from_bytes(header, "big")
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))

'''


def generate_hermes_tools_module(
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    *,
    include_web: bool,
) -> str:
    tools = visible_sandbox_tools(enabled_tools, include_web=include_web)
    chunks = [_UDS_TRANSPORT_HEADER]
    exports: list[str] = []
    for tool_name in tools:
        func_name, signature, doc, args_expr = _TOOL_STUBS[tool_name]
        chunks.append(
            f"def {func_name}({signature}):\n"
            f'    """{doc}"""\n'
            f"    return _call({func_name!r}, {args_expr})\n\n"
        )
        exports.append(func_name)
    chunks.append(f"__all__ = {exports!r}\n")
    return "".join(chunks)


def build_execute_code_description(visible_tools: tuple[str, ...]) -> str:
    tool_list = ", ".join(visible_tools) if visible_tools else "no sandbox tools"
    return (
        "Execute a short Python script locally with constrained access to project tools. "
        "Use this for 3 or more tool calls, loops, filtering, batching, retries, or compressing large intermediate results. "
        "Use direct tools for a single simple operation. "
        "Interactive terminal sessions and background services are not supported. "
        f"Available sandbox tools: {tool_list}."
    )


class ExecuteCodeInput(BaseModel):
    code: str = Field(description="Python code to execute with constrained project tool access.")


@tool("execute_code", args_schema=ExecuteCodeInput)
def execute_code(code: str, runtime: ToolRuntime) -> object:
    """Execute Python code locally with constrained access to selected project tools."""
    return tool_failure(
        "execute_code",
        "execute_code is registered but the local executor is not implemented yet.",
        code="not_implemented",
        runtime=runtime,
    )


SAFE_ENV_EXACT = {"PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME", "VIRTUAL_ENV", "CONDA_PREFIX"}
SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_]*token[A-Za-z0-9_]*=[^\s]+)", re.IGNORECASE)


def _is_safe_env_name(name: str) -> bool:
    upper = name.upper()
    if any(marker in upper for marker in SECRET_SUBSTRINGS):
        return False
    return name in SAFE_ENV_EXACT or any(name.startswith(prefix) for prefix in SAFE_ENV_PREFIXES)


def safe_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if _is_safe_env_name(name)}
    env.update(extra or {})
    return env


def sanitize_output(text: str, *, limit: int) -> tuple[str, bool]:
    cleaned = ANSI_RE.sub("", str(text or ""))
    cleaned = SECRET_VALUE_RE.sub("[REDACTED]", cleaned)
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit] + "\n[truncated]", True


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


def _failure_payload(tool_name: str, message: str, *, code: str, data=None, meta=None) -> dict:
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
    return {
        "ok": True,
        "tool": tool_name,
        "message": "Tool returned a non-standard response.",
        "data": {"value": str(value)},
        "error": None,
        "meta": {},
    }


class CodeExecutionDispatcher:
    def __init__(self, *, runtime: object, visible_tools: tuple[str, ...], max_tool_calls: int = 50) -> None:
        self.runtime = runtime
        self.visible_tools = set(visible_tools)
        self.max_tool_calls = int(max_tool_calls)
        self.tool_calls = 0

    def dispatch(self, tool_name: str, args: dict) -> dict:
        if tool_name not in self.visible_tools:
            return _failure_payload(tool_name, f"Tool is not available in execute_code: {tool_name}", code="tool_not_available")
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            return _failure_payload(tool_name, "execute_code tool call limit exceeded.", code="tool_call_limit_exceeded")
        try:
            result = self._call_tool(tool_name, normalize_rpc_args(tool_name, args))
        except Exception as exc:
            return _failure_payload(tool_name, f"RPC tool dispatch failed: {type(exc).__name__}: {exc}", code="dispatch_error")
        return tool_message_to_rpc_payload(tool_name, result)

    def _call_tool(self, tool_name: str, args: dict) -> object:
        if tool_name in {"read_file", "search_files", "write_file", "patch"}:
            from agent_tools.public import files
            func = getattr(files, tool_name)
            return func(runtime=self.runtime, **args)
        if tool_name == "terminal":
            from agent_tools.public.terminal import terminal
            return terminal(runtime=self.runtime, **args)
        if tool_name in {"web_search", "web_extract"}:
            from agent_tools.public import web
            func = getattr(web, tool_name)
            return func(runtime=self.runtime, **args)
        return _failure_payload(tool_name, f"Tool is not implemented in execute_code: {tool_name}", code="tool_not_implemented")
