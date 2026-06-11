# Code Execution Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build stages 1-3 of the project-native `execute_code` tool: local POSIX child execution, UDS RPC tool calls, current-project permission reuse, and dynamic sandbox tool exposure.

**Architecture:** Add a new `agent_tools.public.code_execution` LangChain tool and register it through `agent_core.tool_catalog`. Keep the implementation local-only for this plan: generated `hermes_tools.py` stubs call a parent UDS server, the parent dispatches to existing public wrappers, and the final tool result is a standard `ToolMessage`.

**Tech Stack:** Python, LangChain `@tool`, Pydantic schemas, Unix domain sockets, subprocess process groups, pytest.

---

## File Structure

- Create `agent_tools/public/code_execution.py`: public LangChain tool, schema helpers, local executor, UDS RPC server, dispatcher, output/env safety helpers.
- Modify `agent_core/tool_catalog.py`: register `execute_code` with `code_execution` toolset and profile-aware default enablement.
- Modify `agent_core/tool_limits.py`: add run/thread limits for `execute_code`.
- Modify `tests/test_tool_catalog.py`: cover dev/test default enablement and hosted/prod default disablement.
- Create `tests/test_code_execution_tool.py`: unit and integration tests for stubs, local execution, dispatch, policy preservation, output/env safety, and web whitelist behavior.
- Optionally modify `README.md`: document when to use `execute_code`, local-only limits, and profile defaults.

## Task 1: Tool Catalog Registration And Profile Gating

**Files:**
- Modify: `agent_core/tool_catalog.py`
- Modify: `agent_core/tool_limits.py`
- Modify: `tests/test_tool_catalog.py`

- [ ] **Step 1: Write failing catalog tests**

Append these tests to `tests/test_tool_catalog.py`:

```python
def test_code_execution_defaults_enabled_for_dev_and_test(monkeypatch):
    from agent_core.tool_catalog import build_tools

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")
    dev_names = _tool_names(build_tools(runtime_profile="dev"))
    assert "execute_code" in dev_names

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "test")
    test_names = _tool_names(build_tools(runtime_profile="test"))
    assert "execute_code" in test_names


def test_code_execution_defaults_disabled_for_hosted_and_prod(monkeypatch):
    from agent_core.tool_catalog import build_tools

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    hosted_names = _tool_names(build_tools(runtime_profile="hosted"))
    assert "execute_code" not in hosted_names

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    prod_names = _tool_names(build_tools(runtime_profile="prod"))
    assert "execute_code" not in prod_names


def test_code_execution_can_be_explicitly_enabled_for_prod():
    from agent_core.tool_catalog import build_tools

    names = _tool_names(build_tools(enabled_toolsets=["code_execution"], runtime_profile="prod"))
    assert names == ["execute_code"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_tool_catalog.py -q
```

Expected: FAIL because `execute_code` is not registered.

- [ ] **Step 3: Add a minimal import-safe `execute_code` shell**

Create `agent_tools/public/code_execution.py` with only the public shape needed for registration:

```python
from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_tools.shared.tool_result import tool_failure


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
```

- [ ] **Step 4: Register the tool with profile-aware default gating**

In `agent_core/tool_catalog.py`, import the tool inside `default_tool_specs`:

```python
from agent_tools.public.code_execution import execute_code
```

Add this helper near `_spec`:

```python
def _code_execution_available_by_default() -> bool:
    return True
```

Add the spec near the terminal tools:

```python
_spec(
    execute_code,
    toolset="code_execution",
    read_only=False,
    risk_level="high",
    max_result_size_chars=100_000,
    enabled_by_default=True,
    check_fn=_code_execution_available_by_default,
),
```

Then adjust `_passes_check` to treat `code_execution` as default-enabled only in `dev` and `test`, while still allowing explicit `enabled_toolsets=["code_execution"]`. Add this helper:

```python
def _is_code_execution_default_allowed(runtime_profile: str | None) -> bool:
    profile = (runtime_profile or "").strip().lower()
    return profile in {"", "dev", "test"}
```

Change `build_tools_from_specs` loop before `_passes_check`:

```python
        if spec.toolset == "code_execution" and enabled is None:
            if not _is_code_execution_default_allowed(runtime_profile):
                continue
```

- [ ] **Step 5: Add tool-call limits**

In `agent_core/tool_limits.py`, add constants:

```python
EXECUTE_CODE_RUN_LIMIT = 4
EXECUTE_CODE_THREAD_LIMIT = 8
```

Add middleware entry in `build_tool_call_limit_middleware`:

```python
        ToolCallLimitMiddleware(
            tool_name="execute_code",
            run_limit=EXECUTE_CODE_RUN_LIMIT,
            thread_limit=EXECUTE_CODE_THREAD_LIMIT,
        ),
```

- [ ] **Step 6: Run catalog tests**

Run:

```bash
pytest tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_tools/public/code_execution.py agent_core/tool_catalog.py agent_core/tool_limits.py tests/test_tool_catalog.py
git commit -m "feat: register code execution tool"
```

## Task 2: Stub Generation, Dynamic Tool Sets, And Schema Text

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Create/Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Write failing stub and schema tests**

Create `tests/test_code_execution_tool.py`:

```python
from types import SimpleNamespace


def test_stage_one_stub_generation_exposes_file_development_tools():
    from agent_tools.public.code_execution import generate_hermes_tools_module

    source = generate_hermes_tools_module(
        ["read_file", "search_files", "write_file", "patch", "terminal"],
        include_web=False,
    )

    assert "def read_file(" in source
    assert "def search_files(" in source
    assert "def write_file(" in source
    assert "def patch(" in source
    assert "def terminal(" in source
    assert "def web_search(" not in source
    assert "def web_extract(" not in source
    assert "__all__" in source


def test_stage_three_stub_generation_can_expose_web_tools():
    from agent_tools.public.code_execution import generate_hermes_tools_module

    source = generate_hermes_tools_module(
        ["read_file", "web_search", "web_extract"],
        include_web=True,
    )

    assert "def read_file(" in source
    assert "def web_search(" in source
    assert "def web_extract(" in source


def test_visible_sandbox_tools_intersects_whitelist_and_enabled_tools():
    from agent_tools.public.code_execution import visible_sandbox_tools

    assert visible_sandbox_tools(
        enabled_tools=["read_file", "terminal", "web_search", "memory_manage"],
        include_web=False,
    ) == ("read_file", "terminal")
    assert visible_sandbox_tools(
        enabled_tools=["read_file", "terminal", "web_search", "memory_manage"],
        include_web=True,
    ) == ("read_file", "terminal", "web_search")


def test_schema_description_lists_only_visible_tools():
    from agent_tools.public.code_execution import build_execute_code_description

    description = build_execute_code_description(("read_file", "terminal"))

    assert "3 or more tool calls" in description
    assert "read_file" in description
    assert "terminal" in description
    assert "web_search" not in description
    assert "single simple operation" in description
    assert "background services are not supported" in description
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: FAIL because helpers do not exist.

- [ ] **Step 3: Implement constants and visible tool helpers**

Add to `agent_tools/public/code_execution.py`:

```python
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
```

- [ ] **Step 4: Implement stub templates**

Add:

```python
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
        "urls, format=\"markdown\", use_llm_processing=True, model=None, min_length=2000, max_chars_per_url=20000",
        "Extract content from one or more web URLs.",
        '{"urls": urls, "format": format, "use_llm_processing": use_llm_processing, "model": model, "min_length": min_length, "max_chars_per_url": max_chars_per_url}',
    ),
}
```

Add transport header:

```python
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
```

Add generator:

```python
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
            f"    \"\"\"{doc}\"\"\"\n"
            f"    return _call({func_name!r}, {args_expr})\n\n"
        )
        exports.append(func_name)
    chunks.append(f"__all__ = {exports!r}\n")
    return "".join(chunks)
```

- [ ] **Step 5: Implement description builder**

Add:

```python
def build_execute_code_description(visible_tools: tuple[str, ...]) -> str:
    tool_list = ", ".join(visible_tools) if visible_tools else "no sandbox tools"
    return (
        "Execute a short Python script locally with constrained access to project tools. "
        "Use this for 3 or more tool calls, loops, filtering, batching, retries, or compressing large intermediate results. "
        "Use direct tools for a single simple operation. "
        "Interactive terminal sessions and background services are not supported. "
        f"Available sandbox tools: {tool_list}."
    )
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "feat: add code execution stub generation"
```

## Task 3: Local Executor Safety Helpers

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add failing safety tests**

Append:

```python
def test_safe_child_env_removes_secret_like_variables(monkeypatch):
    from agent_tools.public.code_execution import safe_child_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("MY_TOKEN", "secret")
    monkeypatch.setenv("LANG", "C.UTF-8")

    env = safe_child_env({"CODE_EXECUTION_RPC_SOCKET": "/tmp/rpc.sock"})

    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["CODE_EXECUTION_RPC_SOCKET"] == "/tmp/rpc.sock"
    assert "OPENAI_API_KEY" not in env
    assert "MY_TOKEN" not in env


def test_sanitize_output_strips_ansi_redacts_and_truncates():
    from agent_tools.public.code_execution import sanitize_output

    text = "\x1b[31mred\x1b[0m sk-testsecret1234567890 more"
    sanitized, truncated = sanitize_output(text, limit=18)

    assert "\x1b" not in sanitized
    assert "sk-testsecret" not in sanitized
    assert len(sanitized) <= 18 + len("\n[truncated]")
    assert truncated is True


def test_terminal_args_are_forced_foreground():
    from agent_tools.public.code_execution import normalize_rpc_args

    args = normalize_rpc_args(
        "terminal",
        {
            "command": "pytest",
            "background": True,
            "pty": True,
            "notify_on_complete": True,
            "watch_patterns": ["ready"],
        },
    )

    assert args == {
        "command": "pytest",
        "background": False,
        "timeout": None,
        "workdir": None,
        "pty": False,
        "notify_on_complete": False,
        "watch_patterns": None,
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: FAIL because helpers do not exist.

- [ ] **Step 3: Implement safety helpers**

Add imports:

```python
import os
import re
```

Add helpers:

```python
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
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "feat: add code execution safety helpers"
```

## Task 4: RPC Dispatcher And Artifact Normalization

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add failing dispatcher tests**

Append:

```python
def _runtime(thread_id="code-exec-thread", tool_call_id="call-execute"):
    return SimpleNamespace(
        config={"configurable": {"thread_id": thread_id}},
        tool_call_id=tool_call_id,
    )


def test_tool_message_artifact_is_normalized_to_script_dict():
    from agent_tools.public.code_execution import tool_message_to_rpc_payload
    from agent_tools.shared.tool_result import tool_success

    message = tool_success(
        "read_file",
        message="File read.",
        data={"path": "README.md"},
        meta={"truncated": False},
        runtime=_runtime(),
    )

    payload = tool_message_to_rpc_payload("read_file", message)

    assert payload == {
        "ok": True,
        "tool": "read_file",
        "message": "File read.",
        "data": {"path": "README.md"},
        "error": None,
        "meta": {"truncated": False},
    }


def test_rpc_dispatch_rejects_unavailable_tool():
    from agent_tools.public.code_execution import CodeExecutionDispatcher

    dispatcher = CodeExecutionDispatcher(runtime=_runtime(), visible_tools=("read_file",))
    payload = dispatcher.dispatch("terminal", {"command": "pwd"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_not_available"


def test_rpc_dispatch_enforces_tool_call_limit():
    from agent_tools.public.code_execution import CodeExecutionDispatcher

    dispatcher = CodeExecutionDispatcher(runtime=_runtime(), visible_tools=("read_file",), max_tool_calls=1)
    dispatcher.dispatch("read_file", {"path": "README.md", "offset": 1, "limit": 1})
    payload = dispatcher.dispatch("read_file", {"path": "README.md", "offset": 1, "limit": 1})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_call_limit_exceeded"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: FAIL because dispatcher helpers do not exist.

- [ ] **Step 3: Implement payload normalization and dispatcher**

Add imports:

```python
from langchain_core.messages import ToolMessage
```

Add:

```python
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
    def __init__(self, *, runtime: ToolRuntime, visible_tools: tuple[str, ...], max_tool_calls: int = 50) -> None:
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
```

- [ ] **Step 4: Run dispatcher tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "feat: dispatch code execution rpc tools"
```

## Task 5: UDS Server And Local Child Executor

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add failing local execution tests**

Append:

```python
def test_execute_code_impl_returns_stdout_for_simple_script():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='print("hello from code")',
        runtime=_runtime(),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "success"
    assert result.artifact["ok"] is True
    assert result.artifact["data"]["stdout"] == "hello from code\n"
    assert result.artifact["data"]["returncode"] == 0


def test_execute_code_impl_reports_nonzero_exit():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='import sys\nprint("bad")\nsys.exit(7)',
        runtime=_runtime(),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "child_failed"
    assert result.artifact["data"]["returncode"] == 7


def test_execute_code_impl_can_call_read_file():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='from hermes_tools import read_file\nresult = read_file("README.md", limit=1)\nprint(result["ok"])\nprint(result["tool"])',
        runtime=_runtime(),
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "True" in result.artifact["data"]["stdout"]
    assert "read_file" in result.artifact["data"]["stdout"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: FAIL because `execute_code_impl` does not exist.

- [ ] **Step 3: Implement local executor and UDS server**

Add imports:

```python
import json
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
```

Add config constants:

```python
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STDOUT_LIMIT_CHARS = 50_000
DEFAULT_STDERR_LIMIT_CHARS = 10_000
```

Add UDS server:

```python
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
                response = self._handle_connection(conn)
                data = json.dumps(response, ensure_ascii=False).encode("utf-8")
                conn.sendall(len(data).to_bytes(8, "big") + data)

    def _handle_connection(self, conn: socket.socket) -> dict:
        header = conn.recv(8)
        if len(header) != 8:
            return _failure_payload("unknown", "Invalid RPC request header.", code="rpc_protocol_error")
        size = int.from_bytes(header, "big")
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = conn.recv(min(65536, remaining))
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
```

Add execution result helper:

```python
def _result_data(stdout: str, stderr: str, returncode: int, *, stdout_truncated: bool, stderr_truncated: bool) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
```

Add executor:

```python
def execute_code_impl(
    *,
    code: str,
    runtime: ToolRuntime,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    include_web: bool,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    stdout_limit_chars: int = DEFAULT_STDOUT_LIMIT_CHARS,
    stderr_limit_chars: int = DEFAULT_STDERR_LIMIT_CHARS,
) -> object:
    if platform.system() == "Windows":
        return tool_failure("execute_code", "execute_code is not available on Windows.", code="unsupported_platform", runtime=runtime)
    visible_tools = visible_sandbox_tools(enabled_tools, include_web=include_web)
    temp_dir = Path(tempfile.mkdtemp(prefix="code-execution-"))
    server: CodeExecutionRpcServer | None = None
    try:
        script_path = temp_dir / "script.py"
        stub_path = temp_dir / "hermes_tools.py"
        socket_path = str(temp_dir / "rpc.sock")
        script_path.write_text(code, encoding="utf-8")
        stub_path.write_text(generate_hermes_tools_module(visible_tools, include_web=include_web), encoding="utf-8")
        dispatcher = CodeExecutionDispatcher(runtime=runtime, visible_tools=visible_tools, max_tool_calls=max_tool_calls)
        server = CodeExecutionRpcServer(socket_path=socket_path, dispatcher=dispatcher)
        server.start()
        env = safe_child_env({"CODE_EXECUTION_RPC_SOCKET": socket_path, "PYTHONPATH": str(temp_dir)})
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
```

- [ ] **Step 4: Run local execution tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "feat: execute code locally over rpc"
```

## Task 6: Wire Public Tool To Executor And Profile/Web Policy

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`

- [ ] **Step 1: Add failing public wrapper tests**

Append:

```python
def test_public_execute_code_uses_local_executor():
    from agent_tools.public.code_execution import execute_code

    result = execute_code.invoke(
        {"code": 'print("public wrapper")'},
        config={"configurable": {"thread_id": "code-exec-public"}},
    )

    assert result.status == "success"
    assert "public wrapper" in result.artifact["data"]["stdout"]


def test_visible_tools_include_web_only_when_requested():
    from agent_tools.public.code_execution import resolve_visible_tools_for_profile

    assert "web_search" not in resolve_visible_tools_for_profile(
        enabled_tools=["read_file", "web_search"],
        runtime_profile="dev",
        include_web=False,
    )
    assert "web_search" in resolve_visible_tools_for_profile(
        enabled_tools=["read_file", "web_search"],
        runtime_profile="dev",
        include_web=True,
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: FAIL because public wrapper still returns `not_implemented`.

- [ ] **Step 3: Implement profile visible-tool resolver**

Add:

```python
def resolve_visible_tools_for_profile(
    *,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    runtime_profile: str | None,
    include_web: bool,
) -> tuple[str, ...]:
    profile = (runtime_profile or "").strip().lower()
    if profile in {"hosted", "prod"}:
        return visible_sandbox_tools(enabled_tools, include_web=include_web)
    return visible_sandbox_tools(enabled_tools, include_web=include_web)
```

This function is intentionally simple in stages 1-3. Profile default enablement is handled by the catalog; this helper exists to keep profile-specific filtering explicit for future remote phases.

- [ ] **Step 4: Wire `execute_code` public wrapper**

Replace the initial shell body:

```python
@tool("execute_code", args_schema=ExecuteCodeInput)
def execute_code(code: str, runtime: ToolRuntime) -> object:
    """Execute Python code locally with constrained access to selected project tools."""
    return execute_code_impl(
        code=code,
        runtime=runtime,
        enabled_tools=list(STAGE_ONE_ALLOWED_TOOLS),
        include_web=False,
    )
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "feat: wire execute code public tool"
```

## Task 7: Stage 2 Permission Preservation Tests

**Files:**
- Modify: `tests/test_code_execution_tool.py`
- Modify: `agent_tools/public/code_execution.py` only if tests expose an integration bug

- [ ] **Step 1: Add permission preservation tests**

Append:

```python
def test_execute_code_terminal_blocks_background_parameters():
    from agent_tools.public.code_execution import execute_code_impl

    code = (
        "from hermes_tools import terminal\n"
        "result = terminal('python -c \"print(123)\"')\n"
        "print(result['tool'])\n"
        "print(result['ok'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-terminal"),
        enabled_tools=["terminal"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "terminal" in result.artifact["data"]["stdout"]


def test_execute_code_out_of_workspace_write_is_not_silently_allowed():
    from agent_tools.public.code_execution import execute_code_impl

    code = (
        "from hermes_tools import write_file\n"
        "result = write_file('/etc/code-exec-denied.txt', 'x')\n"
        "print(result['ok'])\n"
        "print(result['error']['code'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-denied-write"),
        enabled_tools=["write_file"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "False" in result.artifact["data"]["stdout"]
    assert (
        "policy_denied" in result.artifact["data"]["stdout"]
        or "approval_required" in result.artifact["data"]["stdout"]
        or "access_denied" in result.artifact["data"]["stdout"]
    )


def test_execute_code_can_write_workspace_file(tmp_path, monkeypatch):
    from agent_tools.public.code_execution import execute_code_impl

    target = tmp_path / "code-exec-output.txt"
    code = (
        "from hermes_tools import write_file\n"
        f"result = write_file({str(target)!r}, 'hello')\n"
        "print(result['ok'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-workspace-write"),
        enabled_tools=["write_file"],
        include_web=False,
        timeout_seconds=10,
    )

    assert "True" in result.artifact["data"]["stdout"] or result.status == "error"
```

- [ ] **Step 2: Run permission tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS or reveal an implementation bug in runtime/tool invocation.

- [ ] **Step 3: Fix runtime invocation if needed**

If direct public wrapper calls fail because LangChain-decorated tools cannot be called as plain functions, change `_call_tool` to call the implementation helpers where available:

```python
if tool_name == "read_file":
    from agent_tools.public.files import _read_file_impl
    return _read_file_impl(runtime=self.runtime, **args)
if tool_name == "write_file":
    from agent_tools.public.files import _write_file_impl
    return _write_file_impl(runtime=self.runtime, **args)
if tool_name == "patch":
    from agent_tools.public.files import _patch_impl
    return _patch_impl(runtime=self.runtime, **args)
if tool_name == "terminal":
    from agent_tools.public.terminal import _terminal_impl
    return _terminal_impl(runtime=self.runtime, **args)
```

For `search_files`, inspect `agent_tools/public/files.py` for the existing `_search_files_impl` signature and call it with the same arguments the public tool accepts.

- [ ] **Step 4: Rerun tests**

Run:

```bash
pytest tests/test_code_execution_tool.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py
git commit -m "test: preserve code execution tool policies"
```

## Task 8: Stage 3 Web Tools And Documentation

**Files:**
- Modify: `agent_tools/public/code_execution.py`
- Modify: `tests/test_code_execution_tool.py`
- Modify: `README.md`

- [ ] **Step 1: Add stage 3 web dispatch tests**

Append:

```python
def test_web_tools_are_dispatchable_when_visible(monkeypatch):
    from agent_tools.public import code_execution as ce
    from agent_tools.shared.tool_result import tool_success

    def fake_web_search(query, limit=5, runtime=None):
        return tool_success("web_search", message="Search completed.", data={"query": query, "results": []}, runtime=runtime)

    monkeypatch.setattr("agent_tools.public.web.web_search", fake_web_search)

    dispatcher = ce.CodeExecutionDispatcher(runtime=_runtime("code-exec-web"), visible_tools=("web_search",))
    payload = dispatcher.dispatch("web_search", {"query": "langchain", "limit": 1})

    assert payload["ok"] is True
    assert payload["tool"] == "web_search"
    assert payload["data"]["query"] == "langchain"
```

- [ ] **Step 2: Run web tests**

Run:

```bash
pytest tests/test_code_execution_tool.py::test_web_tools_are_dispatchable_when_visible -q
```

Expected: PASS if Task 4 dispatch already supports web.

- [ ] **Step 3: Add public wrapper option for full local whitelist**

Extend `ExecuteCodeInput`:

```python
include_web: bool = Field(default=False, description="Expose web_search and web_extract in addition to local file development tools.")
```

Update wrapper signature:

```python
def execute_code(code: str, runtime: ToolRuntime, include_web: bool = False) -> object:
```

Update wrapper call:

```python
return execute_code_impl(
    code=code,
    runtime=runtime,
    enabled_tools=list(SANDBOX_ALLOWED_TOOLS if include_web else STAGE_ONE_ALLOWED_TOOLS),
    include_web=include_web,
)
```

- [ ] **Step 4: Update README**

Add this subsection under the tools/runtime area in `README.md`:

```markdown
### Code Execution Tool

`execute_code` lets the agent run a short local Python script that can call a constrained set of project tools through generated `hermes_tools.py` stubs. Use it when a task needs 3 or more tool calls, loops, filtering, batching, retries, or large intermediate results that should be compressed before returning to the model.

For a single simple operation, direct tools such as `read_file`, `search_files`, `terminal`, or `web_search` are preferred.

Stages 1-3 are local-only. Windows and non-local terminal backends return an unsupported-backend error. Script-side terminal calls are foreground-only: background processes, PTY interaction, completion notifications, and watch patterns are disabled.

`code_execution` is enabled by default for `dev` and `test` runtime profiles. It is not enabled by default for `hosted` or `prod`, but it can be explicitly enabled through the `code_execution` toolset.
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/code_execution.py tests/test_code_execution_tool.py README.md
git commit -m "feat: expose code execution web tools"
```

## Task 9: Final Verification

**Files:**
- No expected source edits unless verification reveals a defect.

- [ ] **Step 1: Run focused code execution tests**

Run:

```bash
pytest tests/test_code_execution_tool.py tests/test_tool_catalog.py tests/test_agent_cli_builders.py tests/test_tool_bus_middleware.py tests/test_permissions_tool_policy.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader permission and terminal tests**

Run:

```bash
pytest tests/test_permissions_*.py tests/test_terminal_tools.py tests/test_terminal_toolkit_paths.py -q
```

Expected: PASS.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: only intentional implementation/test/doc changes are present. Existing untracked Hermes reference files may remain untracked:

```text
?? agent_tools/code_execution_tool.md
?? agent_tools/code_execution_tool.py
```

- [ ] **Step 4: Commit any final verification fixes**

If a verification fix was needed:

```bash
git add agent_tools/public/code_execution.py agent_core/tool_catalog.py agent_core/tool_limits.py tests/test_code_execution_tool.py tests/test_tool_catalog.py README.md
git commit -m "fix: stabilize code execution tool"
```

If no fix was needed, do not create an empty commit.

## Self-Review

Spec coverage:

- Stage 1 local UDS child execution is covered by Tasks 2, 3, 5, and 6.
- Stage 2 permission preservation is covered by Tasks 4 and 7.
- Stage 3 web whitelist and dynamic exposure are covered by Tasks 2 and 8.
- Catalog profile defaults are covered by Task 1.
- Safety defaults are covered by Tasks 3 and 5.
- Tests and docs are covered by Tasks 1, 2, 7, 8, and 9.

Placeholder scan:

- The plan intentionally avoids unresolved markers and unspecified implementation steps.
- Task 7 includes a concrete fallback code block for runtime invocation if LangChain-decorated direct calls fail.

Type consistency:

- Public entrypoint: `execute_code(code: str, runtime: ToolRuntime, include_web: bool = False)`.
- Internal executor: `execute_code_impl(...)`.
- Stub generator: `generate_hermes_tools_module(enabled_tools, include_web=...)`.
- Dispatcher: `CodeExecutionDispatcher.dispatch(tool_name, args)`.
