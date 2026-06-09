from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from langchain_core.messages import ToolMessage

from agent_core.session_context import RuntimeContext
from agent_tools.file_toolkit.backend_paths import get_backend_path_context
from agent_tools.file_toolkit.redact import redact_sensitive_text
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

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STDOUT_LIMIT_CHARS = 50_000
DEFAULT_STDERR_LIMIT_CHARS = 10_000
MAX_RPC_FRAME_BYTES = 1_000_000
RPC_SOCKET_TIMEOUT_SECONDS = 2.0

_CHILD_POLICY_SITE_CUSTOMIZE = r'''
from __future__ import annotations

import builtins
import os
import socket
import sys
import sysconfig

_SANDBOX_ROOT = os.path.realpath(os.environ.get("CODE_EXECUTION_SANDBOX_ROOT", ""))
_RPC_SOCKET = os.environ.get("CODE_EXECUTION_RPC_SOCKET", "")


def _roots_from_env(name):
    roots = []
    for raw in os.environ.get(name, "").split(os.pathsep):
        if raw:
            roots.append(os.path.realpath(raw))
    return roots


_READ_ROOTS = _roots_from_env("CODE_EXECUTION_ALLOWED_READ_ROOTS")
for _path in (
    sys.prefix,
    getattr(sys, "base_prefix", ""),
    sysconfig.get_path("stdlib") or "",
    sysconfig.get_path("platstdlib") or "",
    sysconfig.get_path("purelib") or "",
    sysconfig.get_path("platlib") or "",
):
    if _path:
        _READ_ROOTS.append(os.path.realpath(_path))


def _under(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _deny(action):
    raise PermissionError(
        f"execute_code policy denied {action}; use hermes_tools RPC wrappers for project files, terminal, and web access."
    )


def _is_write_open(mode, flags):
    mode_text = str(mode or "")
    if any(marker in mode_text for marker in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return False


def _guard_open(path, mode="r", flags=0):
    if isinstance(path, int):
        return
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    path_text = os.fsdecode(path)
    if not path_text:
        return
    if not os.path.isabs(path_text):
        path_text = os.path.join(os.getcwd(), path_text)
    if _is_write_open(mode, flags):
        if _SANDBOX_ROOT and _under(path_text, (_SANDBOX_ROOT,)):
            return
        _deny(f"direct file write to {path_text}")
    if _under(path_text, _READ_ROOTS):
        return
    _deny(f"direct file read from {path_text}")


_original_open = builtins.open


def _policy_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    _guard_open(file, mode=mode)
    return _original_open(file, mode, buffering, encoding, errors, newline, closefd, opener)


builtins.open = _policy_open


def _audit(event, args):
    if event == "open":
        path, mode, flags = (args + (None, None, 0))[:3]
        _guard_open(path, mode=mode, flags=flags)
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "pty.spawn"}:
        _deny(event)
    elif event == "socket.connect":
        sock, address = args
        if getattr(sock, "family", None) == socket.AF_UNIX and address == _RPC_SOCKET:
            return
        _deny("direct socket connect")


sys.addaudithook(_audit)
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


def resolve_visible_tools_for_profile(
    *,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    runtime_profile: str | None,
    include_web: bool,
) -> tuple[str, ...]:
    return visible_sandbox_tools(enabled_tools, include_web=include_web)


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


def build_execute_code_description(visible_tools: tuple[str, ...], *, has_web_tools: bool = False) -> str:
    tool_list = ", ".join(visible_tools) if visible_tools else "no sandbox tools"
    web_note = " Set include_web=True to expose web_search and web_extract." if has_web_tools else ""
    return (
        "Execute a short Python script locally with constrained access to project tools. "
        "Use this for 3 or more tool calls, loops, filtering, batching, retries, or compressing large intermediate results. "
        "Use direct tools for a single simple operation. "
        "Interactive terminal sessions and background services are not supported. "
        f"Available sandbox tools: {tool_list}."
        f"{web_note}"
    )


class ExecuteCodeInput(BaseModel):
    code: str = Field(description="Python code to execute with constrained project tool access.")
    include_web: bool = Field(default=False, description="Expose web_search and web_extract in addition to local file development tools when the caller explicitly allows web access.")


def _default_public_execute_code_result(*, runtime: ToolRuntime) -> object:
    return tool_failure(
        "execute_code",
        "execute_code must be configured by tool_catalog before use so visible tool access matches the current runtime.",
        code="misconfigured_tool",
        runtime=runtime,
    )


@tool("execute_code", args_schema=ExecuteCodeInput)
def execute_code(code: str, runtime: ToolRuntime, include_web: bool = False) -> object:
    """Execute Python code locally with constrained access to selected project tools."""
    return _default_public_execute_code_result(runtime=runtime)


SAFE_ENV_EXACT = {"PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME", "VIRTUAL_ENV", "CONDA_PREFIX"}
SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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
    cleaned = redact_sensitive_text(cleaned)
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit] + "\n[truncated]", True


def _backend_for_runtime(runtime: object) -> tuple[str, str]:
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id or "default"
    backend = get_backend_path_context(task_id)
    return str(backend.env_type or "local"), task_id


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
    return _failure_payload(
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
            return _failure_payload(tool_name, f"Tool is not available in execute_code: {tool_name}", code="tool_not_available")
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            return _failure_payload(tool_name, "execute_code tool call limit exceeded.", code="tool_call_limit_exceeded")
        nested_tool_call_id = self._nested_tool_call_id(tool_name)
        try:
            result = self._call_tool(
                tool_name,
                normalize_rpc_args(tool_name, args),
                nested_tool_call_id=nested_tool_call_id,
            )
        except Exception as exc:
            return _failure_payload(tool_name, f"RPC tool dispatch failed: {type(exc).__name__}: {exc}", code="dispatch_error")
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
        return _failure_payload(tool_name, f"Tool is not implemented in execute_code: {tool_name}", code="tool_not_implemented")


class CodeExecutionRpcServer:
    def __init__(self, *, socket_path: str, dispatcher: CodeExecutionDispatcher) -> None:
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(16)
        self._thread = threading.Thread(target=self._serve, name="code-execution-rpc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(RPC_SOCKET_TIMEOUT_SECONDS)
                response = self._handle_connection(conn)
                data = json.dumps(response, ensure_ascii=False).encode("utf-8")
                conn.sendall(len(data).to_bytes(8, "big") + data)

    def _handle_connection(self, conn: socket.socket) -> dict:
        try:
            header = conn.recv(8)
        except socket.timeout:
            return _failure_payload("unknown", "RPC request timed out.", code="rpc_protocol_error")
        if len(header) != 8:
            return _failure_payload("unknown", "Invalid RPC request header.", code="rpc_protocol_error")
        size = int.from_bytes(header, "big")
        if size > MAX_RPC_FRAME_BYTES:
            return _failure_payload("unknown", "RPC request exceeds maximum frame size.", code="rpc_payload_too_large")
        chunks = []
        remaining = size
        while remaining > 0:
            try:
                chunk = conn.recv(min(65536, remaining))
            except socket.timeout:
                return _failure_payload("unknown", "RPC request timed out.", code="rpc_protocol_error")
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            request = json.loads(b"".join(chunks).decode("utf-8"))
        except json.JSONDecodeError:
            return _failure_payload("unknown", "Invalid RPC JSON request.", code="rpc_json_error")
        tool_name = str(request.get("tool") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        return self.dispatcher.dispatch(tool_name, args)


def _result_data(stdout: str, stderr: str, returncode: int, *, stdout_truncated: bool, stderr_truncated: bool) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def execute_code_impl(
    *,
    code: str,
    runtime: object,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    include_web: bool,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    stdout_limit_chars: int = DEFAULT_STDOUT_LIMIT_CHARS,
    stderr_limit_chars: int = DEFAULT_STDERR_LIMIT_CHARS,
) -> object:
    from agent_tools.shared.tool_result import tool_success

    if platform.system() == "Windows":
        return tool_failure("execute_code", "execute_code is not available on Windows.", code="unsupported_platform", runtime=runtime)

    backend_env_type, runtime_task_id = _backend_for_runtime(runtime)
    if backend_env_type != "local":
        return tool_failure(
            "execute_code",
            f"execute_code only supports the local terminal backend; current backend is {backend_env_type}.",
            code="unsupported_backend",
            runtime=runtime,
            data={"env_type": backend_env_type},
        )

    visible_tools = visible_sandbox_tools(enabled_tools, include_web=include_web)
    temp_dir = Path(tempfile.mkdtemp(prefix="code-execution-"))
    server: CodeExecutionRpcServer | None = None

    try:
        script_path = temp_dir / "script.py"
        stub_path = temp_dir / "hermes_tools.py"
        policy_path = temp_dir / "sitecustomize.py"
        socket_path = str(temp_dir / "rpc.sock")

        script_path.write_text(code, encoding="utf-8")
        stub_path.write_text(generate_hermes_tools_module(visible_tools, include_web=include_web), encoding="utf-8")
        policy_path.write_text(_CHILD_POLICY_SITE_CUSTOMIZE, encoding="utf-8")

        dispatcher = CodeExecutionDispatcher(runtime=runtime, visible_tools=visible_tools, max_tool_calls=max_tool_calls)
        server = CodeExecutionRpcServer(socket_path=socket_path, dispatcher=dispatcher)
        server.start()

        env = safe_child_env({
            "CODE_EXECUTION_RPC_SOCKET": socket_path,
            "CODE_EXECUTION_SANDBOX_ROOT": str(temp_dir),
            "CODE_EXECUTION_ALLOWED_READ_ROOTS": str(temp_dir),
            "PYTHONPATH": str(temp_dir),
        })

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
            stdout_raw, stderr_raw = proc.communicate(timeout=1)
            stdout, stdout_truncated = sanitize_output(stdout_raw, limit=stdout_limit_chars)
            stderr, stderr_truncated = sanitize_output(stderr_raw, limit=stderr_limit_chars)
            return tool_failure(
                "execute_code",
                f"execute_code timed out after {timeout_seconds} seconds.",
                code="timeout",
                data=_result_data(stdout, stderr, -1, stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated),
                runtime=runtime,
            )

        stdout, stdout_truncated = sanitize_output(stdout_raw, limit=stdout_limit_chars)
        stderr, stderr_truncated = sanitize_output(stderr_raw, limit=stderr_limit_chars)
        data = _result_data(stdout, stderr, int(proc.returncode or 0), stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)

        if proc.returncode:
            return tool_failure("execute_code", f"Python script exited with code {proc.returncode}.", code="child_failed", data=data, runtime=runtime)

        return tool_success("execute_code", message="Code executed.", data=data, runtime=runtime, content=stdout or "Code executed.")

    finally:
        if server is not None:
            server.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
