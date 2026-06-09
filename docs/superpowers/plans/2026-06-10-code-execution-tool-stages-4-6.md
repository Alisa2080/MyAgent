# Code Execution Tool Stages 4-6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the project-native `execute_code` tool from local-only UDS execution to Docker file-RPC execution, configurable execution modes, stable lifecycle behavior, and complete tests/docs for stages 4-6.

**Architecture:** Keep `agent_tools.public.code_execution` as the public LangChain tool and compatibility export surface, but move implementation units into `agent_tools/code_execution/`. Local execution keeps the existing UDS transport; Docker execution reuses terminal toolkit's active persistent Docker env and uses a host-visible `/workspace/.code_execution/<run_id>/rpc` directory for file-RPC. All script tool calls still dispatch through existing public wrappers and current permission policy.

**Tech Stack:** Python 3.11, LangChain tools, Pydantic, subprocess/process groups, terminal toolkit Docker environments, JSON file-RPC, pytest.

---

## File Structure

- Create `agent_tools/code_execution/__init__.py`: package marker and narrow exports.
- Create `agent_tools/code_execution/config.py`: parse `CODE_EXECUTION_*` config, mode, limits, env allowlist, secret denylist, warnings.
- Create `agent_tools/code_execution/stubs.py`: shared visible-tool logic and UDS/file-RPC `hermes_tools.py` generation.
- Create `agent_tools/code_execution/dispatch.py`: `CodeExecutionDispatcher`, argument normalization, `ToolMessage` to RPC payload conversion.
- Create `agent_tools/code_execution/local_rpc.py`: current UDS RPC server implementation.
- Create `agent_tools/code_execution/file_rpc.py`: Docker file-RPC parent bridge and file request/response helpers.
- Create `agent_tools/code_execution/runners.py`: `LocalUdsRunner`, `DockerFileRpcRunner`, backend selection, timeout cleanup, output shaping.
- Modify `agent_tools/public/code_execution.py`: keep LangChain tool entrypoint and compatibility exports, delegate to new package.
- Modify `agent_tools/terminal_toolkit/environments/docker.py`: expose host-side workspace path metadata for persistent Docker envs.
- Modify `tests/test_code_execution_tool.py`: update existing tests and add config, stubs, local runner, file-RPC unit coverage.
- Modify `tests/test_tool_catalog.py`: preserve enablement policy tests.
- Create `tests/test_code_execution_docker.py`: Docker integration tests that skip when Docker is unavailable.
- Modify `README.md`: document local/Docker behavior, project/strict modes, Docker persistent `/workspace` requirement, and enablement policy.

## Task 1: Extract Configuration And Stub Generation

**Files:**
- Create: `agent_tools/code_execution/__init__.py`
- Create: `agent_tools/code_execution/config.py`
- Create: `agent_tools/code_execution/stubs.py`
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Write failing config and file-RPC stub tests**

Append these tests to `tests/test_code_execution_tool.py`:

```python
def test_code_execution_config_uses_safe_fallbacks(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig

    monkeypatch.setenv("CODE_EXECUTION_MODE", "invalid")
    monkeypatch.setenv("CODE_EXECUTION_TIMEOUT_SECONDS", "-1")
    monkeypatch.setenv("CODE_EXECUTION_MAX_TOOL_CALLS", "not-an-int")

    config = CodeExecutionConfig.from_sources()

    assert config.mode == "project"
    assert config.timeout_seconds == 300
    assert config.max_tool_calls == 50
    assert any("CODE_EXECUTION_MODE" in warning for warning in config.warnings)
    assert any("CODE_EXECUTION_TIMEOUT_SECONDS" in warning for warning in config.warnings)
    assert any("CODE_EXECUTION_MAX_TOOL_CALLS" in warning for warning in config.warnings)


def test_code_execution_config_denylist_wins_over_allowlist(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig

    monkeypatch.setenv("CODE_EXECUTION_ENV_ALLOWLIST", "OPENAI_API_KEY,SAFE_FLAG")
    monkeypatch.setenv("CODE_EXECUTION_SECRET_DENYLIST", "API_KEY")

    config = CodeExecutionConfig.from_sources()

    assert "SAFE_FLAG" in config.env_allowlist
    assert "OPENAI_API_KEY" in config.env_allowlist
    assert config.env_name_allowed("SAFE_FLAG")
    assert not config.env_name_allowed("OPENAI_API_KEY")


def test_file_rpc_stub_generation_uses_rpc_directory_env():
    from agent_tools.code_execution.stubs import generate_file_rpc_tools_module

    source = generate_file_rpc_tools_module(("read_file", "terminal"))

    assert 'CODE_EXECUTION_RPC_DIR' in source
    assert 'req_' in source
    assert 'res_' in source
    assert 'def read_file(' in source
    assert 'def terminal(' in source
    assert 'def web_search(' not in source
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_code_execution_config_uses_safe_fallbacks tests/test_code_execution_tool.py::test_code_execution_config_denylist_wins_over_allowlist tests/test_code_execution_tool.py::test_file_rpc_stub_generation_uses_rpc_directory_env -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_tools.code_execution'`.

- [ ] **Step 3: Create the code execution package**

Create `agent_tools/code_execution/__init__.py`:

```python
"""Internal implementation units for the public execute_code tool."""

from agent_tools.code_execution.config import CodeExecutionConfig
from agent_tools.code_execution.stubs import (
    SANDBOX_ALLOWED_TOOLS,
    STAGE_ONE_ALLOWED_TOOLS,
    WEB_ALLOWED_TOOLS,
    build_execute_code_description,
    generate_file_rpc_tools_module,
    generate_uds_tools_module,
    visible_sandbox_tools,
)

__all__ = [
    "CodeExecutionConfig",
    "SANDBOX_ALLOWED_TOOLS",
    "STAGE_ONE_ALLOWED_TOOLS",
    "WEB_ALLOWED_TOOLS",
    "build_execute_code_description",
    "generate_file_rpc_tools_module",
    "generate_uds_tools_module",
    "visible_sandbox_tools",
]
```

- [ ] **Step 4: Implement config parsing**

Create `agent_tools/code_execution/config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STDOUT_LIMIT_CHARS = 50_000
DEFAULT_STDERR_LIMIT_CHARS = 10_000
DEFAULT_OUTPUT_LIMIT_CHARS = 100_000
MAX_TIMEOUT_SECONDS = 3_600
MAX_TOOL_CALLS = 500
MAX_OUTPUT_LIMIT_CHARS = 1_000_000
DEFAULT_SAFE_ENV_NAMES = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
})
DEFAULT_SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
DEFAULT_SECRET_DENYLIST = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PASSWD",
    "AUTH",
)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_positive_int(
    *,
    name: str,
    raw: object,
    default: int,
    maximum: int,
    warnings: list[str],
) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warnings.append(f"{name}={raw!r} is invalid; using {default}.")
        return default
    if value <= 0:
        warnings.append(f"{name}={raw!r} must be positive; using {default}.")
        return default
    if value > maximum:
        warnings.append(f"{name}={raw!r} exceeds maximum {maximum}; using {maximum}.")
        return maximum
    return value


@dataclass(frozen=True)
class CodeExecutionConfig:
    mode: str = "project"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    stdout_limit_chars: int = DEFAULT_STDOUT_LIMIT_CHARS
    stderr_limit_chars: int = DEFAULT_STDERR_LIMIT_CHARS
    output_limit_chars: int = DEFAULT_OUTPUT_LIMIT_CHARS
    env_allowlist: tuple[str, ...] = ()
    secret_denylist: tuple[str, ...] = DEFAULT_SECRET_DENYLIST
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_sources(cls, explicit: dict[str, object] | None = None) -> "CodeExecutionConfig":
        explicit = dict(explicit or {})
        warnings: list[str] = []

        raw_mode = explicit.get("mode", os.getenv("CODE_EXECUTION_MODE", "project"))
        mode = str(raw_mode or "project").strip().lower()
        if mode not in {"project", "strict"}:
            warnings.append(f"CODE_EXECUTION_MODE={raw_mode!r} is invalid; using 'project'.")
            mode = "project"

        timeout_seconds = _parse_positive_int(
            name="CODE_EXECUTION_TIMEOUT_SECONDS",
            raw=explicit.get("timeout_seconds", os.getenv("CODE_EXECUTION_TIMEOUT_SECONDS")),
            default=DEFAULT_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
            warnings=warnings,
        )
        max_tool_calls = _parse_positive_int(
            name="CODE_EXECUTION_MAX_TOOL_CALLS",
            raw=explicit.get("max_tool_calls", os.getenv("CODE_EXECUTION_MAX_TOOL_CALLS")),
            default=DEFAULT_MAX_TOOL_CALLS,
            maximum=MAX_TOOL_CALLS,
            warnings=warnings,
        )
        stdout_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_STDOUT_LIMIT_CHARS",
            raw=explicit.get("stdout_limit_chars", os.getenv("CODE_EXECUTION_STDOUT_LIMIT_CHARS")),
            default=DEFAULT_STDOUT_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )
        stderr_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_STDERR_LIMIT_CHARS",
            raw=explicit.get("stderr_limit_chars", os.getenv("CODE_EXECUTION_STDERR_LIMIT_CHARS")),
            default=DEFAULT_STDERR_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )
        output_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_OUTPUT_LIMIT_CHARS",
            raw=explicit.get("output_limit_chars", os.getenv("CODE_EXECUTION_OUTPUT_LIMIT_CHARS")),
            default=DEFAULT_OUTPUT_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )

        explicit_allowlist_raw = explicit.get("env_allowlist", ())
        explicit_allowlist = (
            tuple(str(x).strip() for x in explicit_allowlist_raw if str(x).strip())
            if isinstance(explicit_allowlist_raw, (list, tuple, set))
            else ()
        )
        env_allowlist = tuple(dict.fromkeys([
            *_split_csv(os.getenv("CODE_EXECUTION_ENV_ALLOWLIST")),
            *explicit_allowlist,
        ]))

        explicit_denylist_raw = explicit.get("secret_denylist", ())
        explicit_denylist = (
            tuple(str(x).strip().upper() for x in explicit_denylist_raw if str(x).strip())
            if isinstance(explicit_denylist_raw, (list, tuple, set))
            else ()
        )
        secret_denylist = tuple(dict.fromkeys([
            *DEFAULT_SECRET_DENYLIST,
            *_split_csv(os.getenv("CODE_EXECUTION_SECRET_DENYLIST")),
            *explicit_denylist,
        ]))

        return cls(
            mode=mode,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
            stdout_limit_chars=stdout_limit_chars,
            stderr_limit_chars=stderr_limit_chars,
            output_limit_chars=output_limit_chars,
            env_allowlist=env_allowlist,
            secret_denylist=secret_denylist,
            warnings=tuple(warnings),
        )

    def env_name_allowed(self, name: str) -> bool:
        upper = str(name or "").upper()
        if any(marker and marker.upper() in upper for marker in self.secret_denylist):
            return False
        if name in DEFAULT_SAFE_ENV_NAMES:
            return True
        if any(name.startswith(prefix) for prefix in DEFAULT_SAFE_ENV_PREFIXES):
            return True
        return name in self.env_allowlist
```

- [ ] **Step 5: Implement shared stub generation**

Create `agent_tools/code_execution/stubs.py` by moving the tool constants, `_TOOL_STUBS`, `visible_sandbox_tools`, and `build_execute_code_description` from `agent_tools/public/code_execution.py`. Add these transport-specific generators:

```python
from __future__ import annotations

import textwrap

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

_RPC_DIR = Path(os.environ.get("CODE_EXECUTION_RPC_DIR", ""))
_RPC_TIMEOUT = float(os.environ.get("CODE_EXECUTION_RPC_TIMEOUT_SECONDS", "30"))

def _call(tool_name, args):
    if not str(_RPC_DIR):
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
            raw = res_path.read_text(encoding="utf-8")
            try:
                res_path.unlink()
            except OSError:
                pass
            return json.loads(raw)
        time.sleep(0.05)
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
```

- [ ] **Step 6: Re-export moved helpers from the public module**

In `agent_tools/public/code_execution.py`, replace the duplicated constants and stub helpers with imports:

```python
from agent_tools.code_execution.config import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_STDERR_LIMIT_CHARS,
    DEFAULT_STDOUT_LIMIT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    CodeExecutionConfig,
)
from agent_tools.code_execution.stubs import (
    SANDBOX_ALLOWED_TOOLS,
    STAGE_ONE_ALLOWED_TOOLS,
    WEB_ALLOWED_TOOLS,
    build_execute_code_description,
    generate_hermes_tools_module,
    generate_uds_tools_module,
    visible_sandbox_tools,
)
```

Keep `generate_hermes_tools_module`, `visible_sandbox_tools`, and `build_execute_code_description` importable from `agent_tools.public.code_execution` because current tests and callers use that public path.

- [ ] **Step 7: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_tools/code_execution/__init__.py agent_tools/code_execution/config.py agent_tools/code_execution/stubs.py agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "refactor: split code execution config and stubs"
```

## Task 2: Extract Dispatch And Local UDS Runner

**Files:**
- Create: `agent_tools/code_execution/dispatch.py`
- Create: `agent_tools/code_execution/local_rpc.py`
- Create: `agent_tools/code_execution/runners.py`
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add runner compatibility tests**

Append these tests to `tests/test_code_execution_tool.py`:

```python
def test_local_runner_keeps_direct_subprocess_blocked():
    from agent_tools.code_execution.runners import execute_code_with_backend

    result = execute_code_with_backend(
        code='import subprocess\nsubprocess.run(["echo", "bypass"])\n',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        backend_env_type="local",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "child_failed"
    assert "policy denied" in result.artifact["data"]["stderr"]


def test_execute_code_with_backend_rejects_unknown_non_local_backend():
    from agent_tools.code_execution.runners import execute_code_with_backend

    result = execute_code_with_backend(
        code='print("hello")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        backend_env_type="ssh",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "ssh"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_local_runner_keeps_direct_subprocess_blocked tests/test_code_execution_tool.py::test_execute_code_with_backend_rejects_unknown_non_local_backend -q
```

Expected: FAIL because `agent_tools.code_execution.runners` does not exist.

- [ ] **Step 3: Move dispatch helpers**

Create `agent_tools/code_execution/dispatch.py` with the current `CodeExecutionDispatcher`, `normalize_rpc_args`, `_failure_payload`, and `tool_message_to_rpc_payload` from `agent_tools/public/code_execution.py`. The file must expose:

```python
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
```

- [ ] **Step 4: Move the UDS server**

Create `agent_tools/code_execution/local_rpc.py` with the current `CodeExecutionRpcServer` code, importing `failure_payload` from `dispatch.py`. Keep `MAX_RPC_FRAME_BYTES = 1_000_000` and `RPC_SOCKET_TIMEOUT_SECONDS = 2.0`.

- [ ] **Step 5: Implement runner selection with local behavior**

Create `agent_tools/code_execution/runners.py` with the existing local process execution logic from `execute_code_impl` and this public function:

```python
def execute_code_with_backend(
    *,
    code: str,
    runtime: object,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    include_web: bool,
    backend_env_type: str | None = None,
    config: CodeExecutionConfig | None = None,
) -> object:
    resolved_config = config or CodeExecutionConfig.from_sources()
    env_type, task_id = _backend_for_runtime(runtime)
    if backend_env_type is not None:
        env_type = backend_env_type
    if env_type == "local":
        return LocalUdsRunner().run(
            code=code,
            runtime=runtime,
            enabled_tools=enabled_tools,
            include_web=include_web,
            config=resolved_config,
            task_id=task_id,
        )
    if env_type == "docker":
        return tool_failure(
            "execute_code",
            "Docker execute_code support is not implemented yet.",
            code="unsupported_backend",
            data={"env_type": "docker"},
            runtime=runtime,
        )
    return tool_failure(
        "execute_code",
        f"execute_code does not support the {env_type} terminal backend.",
        code="unsupported_backend",
        data={"env_type": env_type},
        runtime=runtime,
    )
```

`LocalUdsRunner.run(...)` must use:

```python
generate_uds_tools_module(visible_tools)
CodeExecutionDispatcher(runtime=runtime, visible_tools=visible_tools, max_tool_calls=config.max_tool_calls)
CodeExecutionRpcServer(socket_path=socket_path, dispatcher=dispatcher)
safe_child_env(config=config, extra={...})
```

Move `_CHILD_POLICY_SITE_CUSTOMIZE`, `sanitize_output`, `_result_data`, `_backend_for_runtime`, and `safe_child_env` into `runners.py` while preserving their existing behavior.

- [ ] **Step 6: Delegate the public tool to runner selection**

In `agent_tools/public/code_execution.py`, make `execute_code_impl(...)` call:

```python
from agent_tools.code_execution.config import CodeExecutionConfig
from agent_tools.code_execution.runners import execute_code_with_backend


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
    config = CodeExecutionConfig.from_sources(
        {
            "timeout_seconds": timeout_seconds,
            "max_tool_calls": max_tool_calls,
            "stdout_limit_chars": stdout_limit_chars,
            "stderr_limit_chars": stderr_limit_chars,
        }
    )
    return execute_code_with_backend(
        code=code,
        runtime=runtime,
        enabled_tools=enabled_tools,
        include_web=include_web,
        config=config,
    )
```

- [ ] **Step 7: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_tools/code_execution/dispatch.py agent_tools/code_execution/local_rpc.py agent_tools/code_execution/runners.py agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "refactor: split code execution runners"
```

## Task 3: Add Docker Workspace Metadata

**Files:**
- Modify: `agent_tools/terminal_toolkit/environments/docker.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Write focused metadata tests**

Append this test to `tests/test_code_execution_tool.py`:

```python
def test_docker_file_rpc_workspace_requires_persistent_host_workspace():
    from types import SimpleNamespace

    from agent_tools.code_execution.runners import docker_host_workspace_dir

    persistent_env = SimpleNamespace(_persistent=True, _workspace_dir="/tmp/workspace")
    non_persistent_env = SimpleNamespace(_persistent=False, _workspace_dir="/tmp/workspace")
    missing_workspace_env = SimpleNamespace(_persistent=True, _workspace_dir=None)

    assert docker_host_workspace_dir(persistent_env) == "/tmp/workspace"
    assert docker_host_workspace_dir(non_persistent_env) is None
    assert docker_host_workspace_dir(missing_workspace_env) is None
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_docker_file_rpc_workspace_requires_persistent_host_workspace -q
```

Expected: FAIL because `docker_host_workspace_dir` does not exist.

- [ ] **Step 3: Expose Docker persistent workspace metadata**

In `agent_tools/terminal_toolkit/environments/docker.py`, after `_workspace_dir` and `_home_dir` are assigned in `DockerEnvironment.__init__`, add public read-only properties near `cleanup()`:

```python
    @property
    def host_workspace_dir(self) -> str | None:
        """Host-side directory bind-mounted to /workspace for persistent Docker envs."""
        return self._workspace_dir if self._persistent else None

    @property
    def container_workspace_dir(self) -> str:
        """Container-side workspace path used by code execution file-RPC."""
        return "/workspace"
```

These properties return `None` when `/workspace` is supplied only by user volume configuration, because stage 4 requires terminal toolkit's persistent sandbox workspace to be host-visible and owned by the toolkit.

- [ ] **Step 4: Add the runner helper**

In `agent_tools/code_execution/runners.py`, add:

```python
def docker_host_workspace_dir(env: object) -> str | None:
    if not bool(getattr(env, "_persistent", False)):
        return None
    workspace = getattr(env, "host_workspace_dir", None)
    if workspace:
        return str(workspace)
    workspace = getattr(env, "_workspace_dir", None)
    if workspace:
        return str(workspace)
    return None
```

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/terminal_toolkit/environments/docker.py agent_tools/code_execution/runners.py tests/test_code_execution_tool.py
git commit -m "feat: expose docker workspace for code execution"
```

## Task 4: Implement File-RPC Bridge

**Files:**
- Create: `agent_tools/code_execution/file_rpc.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Write file-RPC bridge unit tests**

Append these tests to `tests/test_code_execution_tool.py`:

```python
def test_file_rpc_bridge_dispatches_request(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            return {"ok": True, "tool": tool_name, "message": "done", "data": args, "error": None, "meta": {}}

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), poll_interval_seconds=0.01)
    bridge.start()
    try:
        (rpc_dir / "req_abc.json").write_text(
            json.dumps({"id": "abc", "tool": "read_file", "args": {"path": "README.md"}}),
            encoding="utf-8",
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_abc.json").exists():
            time.sleep(0.01)
        payload = json.loads((rpc_dir / "res_abc.json").read_text(encoding="utf-8"))
    finally:
        bridge.close()

    assert payload["ok"] is True
    assert payload["tool"] == "read_file"
    assert payload["data"] == {"path": "README.md"}
    assert not bridge.is_alive()


def test_file_rpc_bridge_rejects_large_request(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            raise AssertionError("large payload should not dispatch")

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), max_request_bytes=10, poll_interval_seconds=0.01)
    bridge.start()
    try:
        (rpc_dir / "req_big.json").write_text("x" * 100, encoding="utf-8")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_big.json").exists():
            time.sleep(0.01)
        payload = json.loads((rpc_dir / "res_big.json").read_text(encoding="utf-8"))
    finally:
        bridge.close()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "rpc_payload_too_large"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_file_rpc_bridge_dispatches_request tests/test_code_execution_tool.py::test_file_rpc_bridge_rejects_large_request -q
```

Expected: FAIL because `agent_tools.code_execution.file_rpc` does not exist.

- [ ] **Step 3: Implement the file-RPC bridge**

Create `agent_tools/code_execution/file_rpc.py`:

```python
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from agent_tools.code_execution.dispatch import failure_payload

DEFAULT_FILE_RPC_MAX_REQUEST_BYTES = 1_000_000


class FileRpcBridge:
    def __init__(
        self,
        *,
        rpc_dir: str | Path,
        dispatcher,
        max_request_bytes: int = DEFAULT_FILE_RPC_MAX_REQUEST_BYTES,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.rpc_dir = Path(rpc_dir)
        self.dispatcher = dispatcher
        self.max_request_bytes = int(max_request_bytes)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.rpc_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="code-execution-file-rpc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            for req_path in sorted(self.rpc_dir.glob("req_*.json")):
                self._handle_request_path(req_path)
            self._stop.wait(self.poll_interval_seconds)

    def _handle_request_path(self, req_path: Path) -> None:
        request_id = req_path.stem.removeprefix("req_")
        claim_path = req_path.with_name(f".claimed_{request_id}.json")
        try:
            req_path.replace(claim_path)
        except FileNotFoundError:
            return
        except OSError:
            return

        response = self._response_for_claimed_request(claim_path)
        self._write_response(request_id, response)
        try:
            claim_path.unlink()
        except OSError:
            pass

    def _response_for_claimed_request(self, claim_path: Path) -> dict:
        try:
            if claim_path.stat().st_size > self.max_request_bytes:
                return failure_payload("unknown", "RPC request exceeds maximum file size.", code="rpc_payload_too_large")
            request = json.loads(claim_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return failure_payload("unknown", "Invalid RPC JSON request.", code="rpc_json_error")
        except OSError as exc:
            return failure_payload("unknown", f"Failed to read RPC request: {exc}", code="rpc_io_error")

        tool_name = str(request.get("tool") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        return self.dispatcher.dispatch(tool_name, args)

    def _write_response(self, request_id: str, response: dict) -> None:
        res_path = self.rpc_dir / f"res_{request_id}.json"
        tmp_path = self.rpc_dir / f".res_{request_id}.tmp"
        tmp_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(res_path)
```

- [ ] **Step 4: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_file_rpc_bridge_dispatches_request tests/test_code_execution_tool.py::test_file_rpc_bridge_rejects_large_request -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/code_execution/file_rpc.py tests/test_code_execution_tool.py
git commit -m "feat: add code execution file rpc bridge"
```

## Task 5: Implement Docker File-RPC Runner

**Files:**
- Modify: `agent_tools/code_execution/runners.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Write Docker runner unit tests with a fake env**

Append this test to `tests/test_code_execution_tool.py`:

```python
def test_docker_runner_writes_workspace_files_and_executes(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    calls = []

    class FakeEnv:
        _persistent = True
        host_workspace_dir = str(tmp_path / "workspace")
        container_workspace_dir = "/workspace"
        cwd = "/workspace"

        def execute(self, command, cwd="", timeout=None):
            calls.append({"command": command, "cwd": cwd, "timeout": timeout})
            return {"output": "docker hello", "returncode": 0}

    (tmp_path / "workspace").mkdir()

    monkeypatch.setattr(
        "agent_tools.code_execution.runners.terminal_tool.get_or_create_active_env",
        lambda task_id, timeout=None: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code='print("docker hello")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(timeout_seconds=12, warnings=("warn",)),
        task_id="task-docker",
    )

    assert result.artifact["ok"] is True
    assert result.artifact["data"]["stdout"] == "docker hello"
    assert result.artifact["meta"]["backend"] == "docker"
    assert result.artifact["meta"]["warnings"] == ["warn"]
    assert calls[0]["cwd"] == "/workspace"
    assert calls[0]["timeout"] == 12
    assert "/workspace/.code_execution/" in calls[0]["command"]
    assert not any((tmp_path / "workspace" / ".code_execution").glob("*"))


def test_docker_runner_requires_persistent_workspace(monkeypatch):
    from types import SimpleNamespace

    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    monkeypatch.setattr(
        "agent_tools.code_execution.runners.terminal_tool.get_or_create_active_env",
        lambda task_id, timeout=None: SimpleNamespace(_persistent=False, cwd="/workspace"),
    )

    result = DockerFileRpcRunner().run(
        code='print("x")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(),
        task_id="task-docker",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "docker_workspace_unavailable"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_docker_runner_writes_workspace_files_and_executes tests/test_code_execution_tool.py::test_docker_runner_requires_persistent_workspace -q
```

Expected: FAIL because `DockerFileRpcRunner` returns unsupported or does not exist.

- [ ] **Step 3: Implement DockerFileRpcRunner**

In `agent_tools/code_execution/runners.py`, add imports:

```python
import shlex
import uuid

from agent_tools.code_execution.file_rpc import FileRpcBridge
from agent_tools.code_execution.stubs import generate_file_rpc_tools_module
from agent_tools.terminal_toolkit import terminal_tool
```

Add `DockerFileRpcRunner`:

```python
class DockerFileRpcRunner:
    def run(
        self,
        *,
        code: str,
        runtime: object,
        enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
        include_web: bool,
        config: CodeExecutionConfig,
        task_id: str,
    ) -> object:
        from agent_tools.shared.tool_result import tool_failure, tool_success

        env = terminal_tool.get_or_create_active_env(task_id, timeout=config.timeout_seconds)
        host_workspace = docker_host_workspace_dir(env)
        if not host_workspace:
            return tool_failure(
                "execute_code",
                "Docker execute_code requires terminal toolkit persistent /workspace.",
                code="docker_workspace_unavailable",
                data={"env_type": "docker"},
                runtime=runtime,
            )

        host_run_dir = Path(host_workspace) / ".code_execution" / uuid.uuid4().hex
        container_run_dir = f"/workspace/.code_execution/{host_run_dir.name}"
        rpc_dir = host_run_dir / "rpc"
        bridge: FileRpcBridge | None = None

        try:
            rpc_dir.mkdir(parents=True, exist_ok=True)
            visible_tools = visible_sandbox_tools(enabled_tools, include_web=include_web)
            (host_run_dir / "script.py").write_text(code, encoding="utf-8")
            (host_run_dir / "hermes_tools.py").write_text(generate_file_rpc_tools_module(visible_tools), encoding="utf-8")
            (host_run_dir / "sitecustomize.py").write_text(_CHILD_POLICY_SITE_CUSTOMIZE, encoding="utf-8")

            dispatcher = CodeExecutionDispatcher(runtime=runtime, visible_tools=visible_tools, max_tool_calls=config.max_tool_calls)
            bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=dispatcher)
            bridge.start()

            python = "python3"
            quoted_run_dir = shlex.quote(container_run_dir)
            quoted_script = shlex.quote(f"{container_run_dir}/script.py")
            command = (
                f"CODE_EXECUTION_RPC_DIR={shlex.quote(f'{container_run_dir}/rpc')} "
                f"CODE_EXECUTION_RPC_TIMEOUT_SECONDS={shlex.quote(str(min(30, config.timeout_seconds)))} "
                f"CODE_EXECUTION_SANDBOX_ROOT={quoted_run_dir} "
                f"CODE_EXECUTION_ALLOWED_READ_ROOTS={quoted_run_dir} "
                f"PYTHONPATH={quoted_run_dir} "
                f"{python} {quoted_script}"
            )
            cwd = "/workspace" if config.mode == "project" else container_run_dir
            result = env.execute(command, cwd=cwd, timeout=config.timeout_seconds)
            stdout_raw = str(result.get("output", ""))
            returncode = int(result.get("returncode", 0) or 0)
            stdout, stdout_truncated = sanitize_output(stdout_raw, limit=config.stdout_limit_chars)
            stderr, stderr_truncated = sanitize_output("", limit=config.stderr_limit_chars)
            data = _result_data(stdout, stderr, returncode, stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)
            meta = {"backend": "docker", "mode": config.mode, "warnings": list(config.warnings)}

            if returncode:
                return tool_failure(
                    "execute_code",
                    f"Python script exited with code {returncode}.",
                    code="child_failed",
                    data=data,
                    meta=meta,
                    runtime=runtime,
                )
            return tool_success("execute_code", message="Code executed.", data=data, meta=meta, runtime=runtime, content=stdout or "Code executed.")
        except Exception as exc:
            return tool_failure(
                "execute_code",
                f"Docker execute_code failed: {type(exc).__name__}: {exc}",
                code="docker_execution_failed",
                data={"env_type": "docker"},
                meta={"backend": "docker", "mode": config.mode, "warnings": list(config.warnings)},
                runtime=runtime,
            )
        finally:
            if bridge is not None:
                bridge.close()
            shutil.rmtree(host_run_dir, ignore_errors=True)
```

- [ ] **Step 4: Route docker backend to DockerFileRpcRunner**

In `execute_code_with_backend(...)`, replace the Docker unsupported branch with:

```python
    if env_type == "docker":
        return DockerFileRpcRunner().run(
            code=code,
            runtime=runtime,
            enabled_tools=enabled_tools,
            include_web=include_web,
            config=resolved_config,
            task_id=task_id,
        )
```

- [ ] **Step 5: Run unit tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/code_execution/runners.py tests/test_code_execution_tool.py
git commit -m "feat: execute code through docker file rpc"
```

## Task 6: Complete Modes, Output Limits, And Interruption Hooks

**Files:**
- Modify: `agent_tools/code_execution/runners.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add lifecycle and output tests**

Append these tests to `tests/test_code_execution_tool.py`:

```python
def test_sanitize_output_applies_total_limit_after_redaction():
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import sanitize_process_output

    config = CodeExecutionConfig(stdout_limit_chars=80, stderr_limit_chars=80, output_limit_chars=40)
    data = sanitize_process_output(
        stdout_raw="token sk-test1234567890 visible tail",
        stderr_raw="stderr text",
        returncode=0,
        config=config,
    )

    assert "sk-test1234567890" not in data["stdout"]
    assert len(data["stdout"]) + len(data["stderr"]) <= 40
    assert data["stdout_truncated"] is True


def test_strict_docker_mode_uses_run_directory(monkeypatch, tmp_path):
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    calls = []

    class FakeEnv:
        _persistent = True
        host_workspace_dir = str(tmp_path / "workspace")
        container_workspace_dir = "/workspace"
        cwd = "/workspace/project"

        def execute(self, command, cwd="", timeout=None):
            calls.append({"command": command, "cwd": cwd, "timeout": timeout})
            return {"output": "strict", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        "agent_tools.code_execution.runners.terminal_tool.get_or_create_active_env",
        lambda task_id, timeout=None: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code='print("strict")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(mode="strict"),
        task_id="task-docker",
    )

    assert result.artifact["ok"] is True
    assert calls[0]["cwd"].startswith("/workspace/.code_execution/")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py::test_sanitize_output_applies_total_limit_after_redaction tests/test_code_execution_tool.py::test_strict_docker_mode_uses_run_directory -q
```

Expected: FAIL because `sanitize_process_output` does not exist or strict mode still uses `/workspace`.

- [ ] **Step 3: Add shared process output sanitizer**

In `agent_tools/code_execution/runners.py`, add:

```python
def sanitize_process_output(
    *,
    stdout_raw: str,
    stderr_raw: str,
    returncode: int,
    config: CodeExecutionConfig,
) -> dict:
    stdout, stdout_truncated = sanitize_output(stdout_raw, limit=config.stdout_limit_chars)
    stderr, stderr_truncated = sanitize_output(stderr_raw, limit=config.stderr_limit_chars)
    combined_len = len(stdout) + len(stderr)
    if combined_len > config.output_limit_chars:
        stdout_budget = min(
            len(stdout),
            max(0, config.output_limit_chars - min(len(stderr), config.output_limit_chars // 2)),
        )
        stderr_budget = max(0, config.output_limit_chars - stdout_budget)
        if len(stdout) > stdout_budget:
            stdout = stdout[:stdout_budget]
            stdout_truncated = True
        if len(stderr) > stderr_budget:
            stderr = stderr[:stderr_budget]
            stderr_truncated = True
    return _result_data(
        stdout,
        stderr,
        int(returncode),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
```

Use `sanitize_process_output(...)` in both local and Docker runners instead of duplicating stdout/stderr truncation.

- [ ] **Step 4: Integrate terminal execution scope**

In `agent_tools/code_execution/runners.py`, import:

```python
from contextlib import nullcontext
from agent_core.terminal_lifecycle import terminal_execution_scope
from agent_core.session_context import RuntimeContext
```

Add:

```python
def _execution_scope_for_runtime(runtime: object):
    context = RuntimeContext.from_runtime(runtime)
    thread_id = getattr(context, "thread_id", None)
    return terminal_execution_scope(thread_id) if thread_id else nullcontext()
```

Wrap local `proc.communicate(...)` and Docker `env.execute(...)` waits with:

```python
with _execution_scope_for_runtime(runtime):
    ...
```

- [ ] **Step 5: Ensure strict mode uses isolated cwd**

In `DockerFileRpcRunner.run(...)`, keep:

```python
cwd = "/workspace" if config.mode == "project" else container_run_dir
```

In `LocalUdsRunner.run(...)`, use:

```python
cwd = os.getcwd() if config.mode == "project" else str(temp_dir)
```

Pass that `cwd` to `subprocess.Popen(...)`.

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_tools/code_execution/runners.py tests/test_code_execution_tool.py
git commit -m "feat: complete code execution modes and limits"
```

## Task 7: Add Docker Integration Tests

**Files:**
- Create: `tests/test_code_execution_docker.py`

- [ ] **Step 1: Create Docker integration tests**

Create `tests/test_code_execution_docker.py`:

```python
import json
import shutil

import pytest


pytestmark = pytest.mark.skipif(shutil.which("docker") is None and shutil.which("podman") is None, reason="Docker or Podman is required")


def _artifact(result):
    return getattr(result, "artifact", result)


def test_execute_code_docker_simple_script(monkeypatch, tmp_path):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))

    result = execute_code_impl(
        code='print("docker ok")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        timeout_seconds=60,
    )

    artifact = _artifact(result)
    assert artifact["ok"] is True
    assert "docker ok" in artifact["data"]["stdout"]
    assert artifact["meta"]["backend"] == "docker"


def test_execute_code_docker_file_rpc_read_file(monkeypatch, tmp_path):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))

    result = execute_code_impl(
        code=(
            "from hermes_tools import read_file\n"
            "result = read_file('README.md', limit=5)\n"
            "print(result['ok'])\n"
        ),
        runtime=None,
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=90,
    )

    artifact = _artifact(result)
    assert artifact["ok"] is True
    assert "True" in artifact["data"]["stdout"]


def test_execute_code_docker_timeout_cleans_run_dir(monkeypatch, tmp_path):
    from agent_tools.public.code_execution import execute_code_impl

    sandbox_dir = tmp_path / "sandboxes"
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(sandbox_dir))

    result = execute_code_impl(
        code="import time\ntime.sleep(5)",
        runtime=None,
        enabled_tools=[],
        include_web=False,
        timeout_seconds=1,
    )

    artifact = _artifact(result)
    assert artifact["ok"] is False
    assert artifact["error"]["code"] in {"timeout", "child_failed", "docker_execution_failed"}
    assert not list(sandbox_dir.glob("docker/*/workspace/.code_execution/*"))
```

- [ ] **Step 2: Run Docker integration tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_docker.py -q
```

Expected: PASS when Docker or Podman is available; SKIP otherwise.

- [ ] **Step 3: Run targeted full suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_code_execution_docker.py tests/test_tool_catalog.py tests/test_agent_cli_builders.py tests/test_tool_bus_middleware.py tests/test_permissions_tool_policy.py -q
```

Expected: PASS, with Docker tests skipped if Docker is unavailable.

- [ ] **Step 4: Commit**

```bash
git add tests/test_code_execution_docker.py
git commit -m "test: cover docker code execution"
```

## Task 8: Update Documentation And Enablement Tests

**Files:**
- Modify: `README.md`
- Modify: `tests/test_tool_catalog.py`

- [ ] **Step 1: Add catalog policy regression test**

Append this test to `tests/test_tool_catalog.py`:

```python
def test_code_execution_hosted_prod_still_require_explicit_toolset(monkeypatch):
    from agent_core.tool_catalog import build_tools

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    assert "execute_code" not in _tool_names(build_tools(runtime_profile="hosted"))
    assert "execute_code" in _tool_names(build_tools(enabled_toolsets=["code_execution"], runtime_profile="hosted"))

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    assert "execute_code" not in _tool_names(build_tools(runtime_profile="prod"))
    assert "execute_code" in _tool_names(build_tools(enabled_toolsets=["code_execution"], runtime_profile="prod"))
```

- [ ] **Step 2: Run catalog test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_catalog.py::test_code_execution_hosted_prod_still_require_explicit_toolset -q
```

Expected: PASS.

- [ ] **Step 3: Update README code execution section**

In `README.md`, replace the existing code execution notes with:

```markdown
## Code Execution Tool

`execute_code` runs short Python scripts that can call a constrained set of
project tools through `hermes_tools.py`. It is intended for 3+ tool calls,
loops, filtering, batching, retries, and compressing large intermediate
results. Use direct tools for a single simple operation.

Backend behavior:

- `local`: runs a child Python process and uses Unix domain socket RPC.
- `docker`: reuses the active terminal toolkit Docker environment and uses
  file-based RPC under `/workspace/.code_execution/<run_id>/`.
- Other non-local terminal backends currently return `unsupported_backend`.

Docker requirements:

- `TERMINAL_CONTAINER_PERSISTENT=true`
- terminal toolkit must own a persistent host-visible `/workspace` sandbox
- hosted/prod default terminal environment resolves to Docker, but
  `code_execution` must still be explicitly enabled in hosted/prod

Execution modes:

- `project` is the default and uses the session working directory/environment.
- `strict` uses an isolated run directory and accesses project files only
  through RPC tools.

Configuration:

- `CODE_EXECUTION_MODE=project|strict`
- `CODE_EXECUTION_TIMEOUT_SECONDS`
- `CODE_EXECUTION_MAX_TOOL_CALLS`
- `CODE_EXECUTION_STDOUT_LIMIT_CHARS`
- `CODE_EXECUTION_STDERR_LIMIT_CHARS`
- `CODE_EXECUTION_OUTPUT_LIMIT_CHARS`
- `CODE_EXECUTION_ENV_ALLOWLIST`
- `CODE_EXECUTION_SECRET_DENYLIST`

Scripts do not directly inherit API key, token, password, credential, auth, or
similar secret environment variables. Script-side terminal calls are
foreground-only; background processes, PTY interaction, completion
notifications, and watch patterns are disabled.

Default enablement:

- `dev` and `test`: enabled by default
- `hosted` and `prod`: disabled by default, explicit `code_execution` toolset
  opt-in required
```

- [ ] **Step 4: Run docs-related tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_catalog.py tests/test_code_execution_tool.py -q
git diff --check
```

Expected: pytest PASS and `git diff --check` has no output.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_tool_catalog.py
git commit -m "docs: document docker code execution"
```

## Task 9: Final Verification And Review

**Files:**
- Validate all files changed by Tasks 1-8.

- [ ] **Step 1: Run complete targeted verification**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_code_execution_tool.py tests/test_code_execution_docker.py tests/test_tool_catalog.py tests/test_agent_cli_builders.py tests/test_tool_bus_middleware.py tests/test_permissions_tool_policy.py tests/test_terminal_lifecycle.py -q
git diff --check
```

Expected: pytest PASS, Docker tests SKIP only when Docker or Podman is unavailable, and `git diff --check` has no output.

- [ ] **Step 2: Manually inspect public compatibility exports**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python - <<'PY'
from agent_tools.public.code_execution import (
    build_execute_code_description,
    execute_code,
    execute_code_impl,
    generate_hermes_tools_module,
    visible_sandbox_tools,
)
print(execute_code.name)
print(callable(execute_code_impl))
print("read_file" in generate_hermes_tools_module(["read_file"], include_web=False))
print(visible_sandbox_tools(["read_file"], include_web=False))
print("Available sandbox tools" in build_execute_code_description(("read_file",)))
PY
```

Expected output includes:

```text
execute_code
True
True
('read_file',)
True
```

- [ ] **Step 3: Request code review**

Use `superpowers:requesting-code-review` to review:

- Docker file-RPC cleanup and timeout paths.
- Secret environment denylist and output redaction.
- Hosted/prod default enablement.
- Compatibility exports from `agent_tools.public.code_execution`.
- Permission preservation for nested file, patch, terminal, and web calls.

- [ ] **Step 4: Commit review fixes**

If review finds issues, fix them with focused tests and commit:

```bash
git add <changed-files>
git commit -m "fix: address code execution review"
```

- [ ] **Step 5: Finish the branch**

Use `superpowers:finishing-a-development-branch` after tests and review pass.
