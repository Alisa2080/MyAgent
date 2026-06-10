from __future__ import annotations

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

_FILE_RPC_TRANSPORT_HEADER = r'''
import json
import os
import time
import uuid
from pathlib import Path

_RPC_DIR_RAW = os.environ.get("CODE_EXECUTION_RPC_DIR", "")
_RPC_DIR = Path(_RPC_DIR_RAW) if _RPC_DIR_RAW else None
_RPC_TIMEOUT = float(os.environ.get("CODE_EXECUTION_RPC_TIMEOUT_SECONDS", "30"))
_RPC_MAX_RESPONSE_BYTES = int(os.environ.get("CODE_EXECUTION_RPC_MAX_RESPONSE_BYTES", "1000000"))

def _call(tool_name, args):
    if _RPC_DIR is None:
        return {"ok": False, "tool": tool_name, "message": "RPC directory is not configured.", "error": {"code": "rpc_unavailable", "message": "RPC directory is not configured."}, "data": None, "meta": {}}
    request_id = uuid.uuid4().hex
    req_path = _RPC_DIR / f"req_{request_id}.json"
    tmp_path = _RPC_DIR / f".req_{request_id}.tmp"
    res_path = _RPC_DIR / f"res_{request_id}.json"
    payload = {"id": request_id, "tool": tool_name, "args": args}
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(req_path)
    deadline = time.monotonic() + _RPC_TIMEOUT
    while time.monotonic() < deadline:
        if res_path.exists():
            if res_path.stat().st_size > _RPC_MAX_RESPONSE_BYTES:
                try:
                    res_path.unlink()
                except OSError:
                    pass
                return {"ok": False, "tool": tool_name, "message": "RPC response exceeds maximum file size.", "error": {"code": "rpc_response_too_large", "message": "RPC response exceeds maximum file size."}, "data": None, "meta": {}}
            raw = res_path.read_text(encoding="utf-8")
            try:
                res_path.unlink()
            except OSError:
                pass
            return json.loads(raw)
        time.sleep(0.05)
    try:
        req_path.unlink()
    except OSError:
        pass
    return {"ok": False, "tool": tool_name, "message": "RPC response timed out.", "error": {"code": "rpc_timeout", "message": "RPC response timed out."}, "data": None, "meta": {}}

'''


def visible_sandbox_tools(
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    *,
    include_web: bool,
) -> tuple[str, ...]:
    allowed = set(STAGE_ONE_ALLOWED_TOOLS)
    if include_web:
        allowed.update(WEB_ALLOWED_TOOLS)
    enabled = allowed if enabled_tools is None else set(enabled_tools)
    return tuple(name for name in TOOL_ORDER if name in allowed and name in enabled)


def _render_tools(transport_header: str, tools: tuple[str, ...]) -> str:
    chunks = [transport_header]
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


def generate_uds_tools_module(visible_tools: tuple[str, ...]) -> str:
    return _render_tools(_UDS_TRANSPORT_HEADER, tuple(visible_tools))


def generate_file_rpc_tools_module(visible_tools: tuple[str, ...]) -> str:
    return _render_tools(_FILE_RPC_TRANSPORT_HEADER, tuple(visible_tools))


def generate_hermes_tools_module(
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    *,
    include_web: bool,
) -> str:
    return generate_uds_tools_module(visible_sandbox_tools(enabled_tools, include_web=include_web))


def build_execute_code_description(visible_tools: tuple[str, ...], *, has_web_tools: bool = False) -> str:
    tool_list = ", ".join(visible_tools) if visible_tools else "no sandbox tools"
    web_note = " Set include_web=True to expose web_search and web_extract." if has_web_tools else ""
    return (
        "Execute a short Python script with constrained access to project tools. "
        "Use this for 3 or more tool calls, loops, filtering, batching, retries, or compressing large intermediate results. "
        "Use direct tools for a single simple operation. "
        "Interactive terminal sessions and background services are not supported. "
        f"Available sandbox tools: {tool_list}."
        f"{web_note}"
    )
