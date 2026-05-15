# Hermes Thread Task ID Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use LangGraph execution `thread_id` as the stable session source for Hermes `task_id`, so terminal state and future background processes are isolated per conversation.

**Architecture:** Tools receive hidden LangChain `ToolRuntime` when available, derive a sanitized Hermes task id from `runtime.execution_info.thread_id`, and fall back to `"default"` in local tests or older runtimes. The model never sees or controls `task_id`; shell execution and future `terminal/process` wrappers use the same helper.

**Tech Stack:** Python, LangChain `@tool`, LangChain `ToolRuntime`, LangGraph execution info, Hermes terminal toolkit, pytest-compatible unit tests.

---

## Source Notes

- LangChain runtime docs show that tools can declare `runtime: ToolRuntime` and read `runtime.execution_info.thread_id`; the parameter is injected and hidden from the model.
- LangChain tools docs say `ToolRuntime` exposes state, context, store, stream writer, config, tool call id, execution info, and server info.
- LangGraph persistence docs define `thread_id` as the checkpoint thread identifier passed under `{"configurable": {"thread_id": "..."}}`.
- LangSmith/LangGraph thread docs describe a thread as a persistent conversation container with a unique thread ID.
- `runtime.execution_info` requires `deepagents>=0.5.0` or `langgraph>=1.1.5`; this plan includes a fallback for environments that do not provide it.

## File Structure

- Create `agent_core/session_context.py`: derive stable, safe Hermes task ids from LangChain `ToolRuntime`, raw thread ids, or fallback values.
- Modify `agent_tools/shell.py`: add hidden optional runtime parameter to `execute_command`, call the session helper, and pass the derived task id to `run_foreground_command`.
- Modify `agent_tools/hermes_shell_adapter.py`: keep the explicit `task_id` parameter and validate it defensively before calling Hermes.
- Create `tests/test_session_context.py`: unit-test task id derivation, sanitization, deterministic hashing, and fallback behavior without requiring LangChain.
- Create `tests/test_shell_task_id.py`: unit-test that `execute_command` forwards the derived task id to the Hermes adapter using stubs for LangChain/Pydantic.
- Optionally modify future `terminal/process` wrappers in a later plan; do not expose Hermes `task_id` to the model in this task.

---

### Task 1: Add Session Task ID Helper

**Files:**
- Create: `agent_core/session_context.py`
- Test: `tests/test_session_context.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_context.py`:

```python
from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_runtime, hermes_task_id_from_thread_id


def test_hermes_task_id_is_deterministic_and_prefixed():
    first = hermes_task_id_from_thread_id("thread-123")
    second = hermes_task_id_from_thread_id("thread-123")

    assert first == second
    assert first.startswith("lg_")
    assert len(first) == 27


def test_hermes_task_id_does_not_embed_raw_thread_id():
    task_id = hermes_task_id_from_thread_id("user@example.com/session/abc")

    assert "user@example.com" not in task_id
    assert "/" not in task_id
    assert task_id.startswith("lg_")


def test_missing_thread_id_falls_back_to_default():
    assert hermes_task_id_from_thread_id(None) == "default"
    assert hermes_task_id_from_thread_id("") == "default"
    assert hermes_task_id_from_runtime(None) == "default"


def test_runtime_execution_info_thread_id_is_used():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert hermes_task_id_from_runtime(runtime) == hermes_task_id_from_thread_id("thread-from-runtime")


def test_runtime_config_thread_id_is_fallback_when_execution_info_missing():
    runtime = SimpleNamespace(
        execution_info=None,
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert hermes_task_id_from_runtime(runtime) == hermes_task_id_from_thread_id("thread-from-config")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_context.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.session_context'`.

- [ ] **Step 3: Implement the helper**

Create `agent_core/session_context.py`:

```python
from __future__ import annotations

from hashlib import sha256
from typing import Any


_TASK_ID_PREFIX = "lg_"
_TASK_ID_HASH_CHARS = 24
_FALLBACK_TASK_ID = "default"


def hermes_task_id_from_thread_id(thread_id: str | None) -> str:
    """Return a path-safe Hermes task id derived from a LangGraph thread id."""
    if not thread_id:
        return _FALLBACK_TASK_ID
    digest = sha256(str(thread_id).encode("utf-8")).hexdigest()[:_TASK_ID_HASH_CHARS]
    return f"{_TASK_ID_PREFIX}{digest}"


def hermes_task_id_from_runtime(runtime: Any | None) -> str:
    """Extract LangGraph thread identity from ToolRuntime-like objects."""
    thread_id = _thread_id_from_execution_info(runtime)
    if thread_id:
        return hermes_task_id_from_thread_id(thread_id)

    thread_id = _thread_id_from_config(runtime)
    return hermes_task_id_from_thread_id(thread_id)


def _thread_id_from_execution_info(runtime: Any | None) -> str | None:
    execution_info = getattr(runtime, "execution_info", None)
    if execution_info is None:
        return None
    value = getattr(execution_info, "thread_id", None)
    return str(value) if value else None


def _thread_id_from_config(runtime: Any | None) -> str | None:
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("thread_id")
    return str(value) if value else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_context.py -v`

Expected: PASS for all tests in `tests/test_session_context.py`.

- [ ] **Step 5: Commit**

```bash
git add agent_core/session_context.py tests/test_session_context.py
git commit -m "feat: derive hermes task ids from langgraph threads"
```

---

### Task 2: Inject Session Task ID Into execute_command

**Files:**
- Modify: `agent_tools/shell.py`
- Test: `tests/test_shell_task_id.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shell_task_id.py`:

```python
import importlib
import sys
import types
from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_thread_id


def _install_langchain_and_pydantic_stubs(monkeypatch):
    langchain = types.ModuleType("langchain")
    tools = types.ModuleType("langchain.tools")

    def tool(name=None, args_schema=None):
        def decorate(func):
            func.name = name or func.__name__
            func.args_schema = args_schema
            return func
        return decorate

    class ToolRuntime:
        pass

    tools.tool = tool
    tools.ToolRuntime = ToolRuntime
    langchain.tools = tools

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    def Field(*args, **kwargs):
        return None

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field

    monkeypatch.setitem(sys.modules, "langchain", langchain)
    monkeypatch.setitem(sys.modules, "langchain.tools", tools)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)


def _import_shell(monkeypatch):
    _install_langchain_and_pydantic_stubs(monkeypatch)
    sys.modules.pop("agent_tools.shell", None)
    return importlib.import_module("agent_tools.shell")


def test_execute_command_forwards_runtime_thread_as_task_id(monkeypatch):
    shell = _import_shell(monkeypatch)
    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append(
            {
                "command": command,
                "workdir": workdir,
                "timeout": timeout,
                "task_id": task_id,
            }
        )
        return {"output": "ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-abc"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    result = shell.execute_command("python -c \"print('ok')\"", runtime=runtime)

    assert '"status": "ok"' in result
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("thread-abc")


def test_execute_command_falls_back_to_default_without_runtime(monkeypatch):
    shell = _import_shell(monkeypatch)
    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append(task_id)
        return {"output": "ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)

    result = shell.execute_command("python -c \"print('ok')\"")

    assert '"status": "ok"' in result
    assert calls == ["default"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_shell_task_id.py -v`

Expected: FAIL with `TypeError` because `execute_command` does not accept `runtime`.

- [ ] **Step 3: Modify imports in `agent_tools/shell.py`**

Update the import block near the top of `agent_tools/shell.py`:

```python
import re
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.session_context import hermes_task_id_from_runtime
from agent_core.workspace import WORKDIR
from agent_tools.common import truncate
from agent_tools.hermes_shell_adapter import run_foreground_command
from agent_tools.tool_output import tool_error, tool_ok
```

- [ ] **Step 4: Modify `execute_command` signature and adapter call**

Change the function definition and `run_foreground_command` call in `agent_tools/shell.py`:

```python
@tool("execute_command", args_schema=ExecuteCommandInput)
def execute_command(command: str, runtime: ToolRuntime | None = None) -> str:
    """Execute a shell command inside the workspace. Returns JSON: status, message, data."""
    reason = _is_dangerous(command)
    if reason:
        return tool_error("execute_command", f"Dangerous command blocked: {reason}", code="blocked_command")
    if _has_path_outside_workspace(command):
        return tool_error("execute_command", "Command references absolute paths outside the workspace", code="invalid_path")
    payload = run_foreground_command(
        command,
        workdir=str(WORKDIR),
        timeout=120,
        task_id=hermes_task_id_from_runtime(runtime),
    )
    if not isinstance(payload, dict):
        return tool_error("execute_command", "Terminal backend returned an invalid response.", code="invalid_response")
```

Keep the rest of the function body unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_shell_task_id.py tests/test_session_context.py -v`

Expected: PASS for both test files.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/shell.py tests/test_shell_task_id.py
git commit -m "feat: bind execute_command to langgraph thread task id"
```

---

### Task 3: Harden Hermes Adapter Task ID Handling

**Files:**
- Modify: `agent_tools/hermes_shell_adapter.py`
- Test: `tests/test_hermes_shell_adapter_task_id.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hermes_shell_adapter_task_id.py`:

```python
import json

from agent_tools import hermes_shell_adapter


def test_adapter_forwards_task_id_to_hermes(monkeypatch, tmp_path):
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok", "exit_code": 0, "error": None})

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(hermes_shell_adapter, "run_terminal", fake_run_terminal)

    result = hermes_shell_adapter.run_foreground_command(
        "python -c \"print('ok')\"",
        workdir=str(tmp_path),
        task_id="lg_abc123",
    )

    assert result["exit_code"] == 0
    assert calls[0]["task_id"] == "lg_abc123"


def test_adapter_replaces_empty_task_id_with_default(monkeypatch, tmp_path):
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok", "exit_code": 0, "error": None})

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(hermes_shell_adapter, "run_terminal", fake_run_terminal)

    result = hermes_shell_adapter.run_foreground_command(
        "python -c \"print('ok')\"",
        workdir=str(tmp_path),
        task_id="",
    )

    assert result["exit_code"] == 0
    assert calls[0]["task_id"] == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hermes_shell_adapter_task_id.py -v`

Expected: The empty task id test FAILS because the adapter forwards `""` unchanged.

- [ ] **Step 3: Add task id normalization to adapter**

Modify `agent_tools/hermes_shell_adapter.py`:

```python
import json
import os
from pathlib import Path

from agent_tools.hermes_terminal_toolkit.terminal import run_terminal


def _normalize_task_id(task_id: str | None) -> str:
    return str(task_id).strip() if task_id and str(task_id).strip() else "default"


def run_foreground_command(
    command: str,
    *,
    workdir: str,
    timeout: int = 120,
    task_id: str = "default",
) -> dict:
    env_type = os.getenv("TERMINAL_ENV", "local").strip().lower() or "local"
    if env_type != "local":
        return {
            "output": "",
            "exit_code": -1,
            "error": (
                "execute_command currently supports only the local Hermes backend. "
                f"Found TERMINAL_ENV={env_type!r}."
            ),
            "status": "error",
        }

    raw = run_terminal(
        command=command,
        background=False,
        timeout=timeout,
        task_id=_normalize_task_id(task_id),
        workdir=str(Path(workdir).resolve()),
        pty=False,
        force=True,
    )
```

Keep the JSON parsing logic unchanged.

- [ ] **Step 4: Run adapter tests**

Run: `python -m pytest tests/test_hermes_shell_adapter_task_id.py -v`

Expected: PASS for both tests.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/hermes_shell_adapter.py tests/test_hermes_shell_adapter_task_id.py
git commit -m "fix: normalize hermes adapter task ids"
```

---

### Task 4: Add Integration Smoke Tests for Real Hermes Local Execution

**Files:**
- Create: `tests/test_execute_command_runtime_smoke.py`

- [ ] **Step 1: Write smoke tests**

Create `tests/test_execute_command_runtime_smoke.py`:

```python
import json
import sys
from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_thread_id
from agent_tools.shell import execute_command


def _decode_tool_result(raw: str) -> dict:
    return json.loads(raw)


def test_execute_command_uses_runtime_thread_with_real_adapter():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="smoke-thread-1"),
        config={"configurable": {"thread_id": "ignored"}},
    )

    raw = execute_command(
        f"{sys.executable} -c \"print('shell-smoke-ok')\"",
        runtime=runtime,
    )
    payload = _decode_tool_result(raw)

    assert payload["status"] == "ok"
    assert payload["data"]["exit_code"] == 0
    assert "shell-smoke-ok" in payload["data"]["output"]
    assert hermes_task_id_from_thread_id("smoke-thread-1").startswith("lg_")


def test_execute_command_still_blocks_dangerous_commands_before_hermes():
    raw = execute_command("sudo ls")
    payload = _decode_tool_result(raw)

    assert payload["status"] == "error"
    assert payload["code"] == "blocked_command"
```

- [ ] **Step 2: Run smoke tests**

Run: `python -m pytest tests/test_execute_command_runtime_smoke.py -v`

Expected: PASS if LangChain, Pydantic, and pytest are installed. If the environment lacks LangChain/Pydantic, record the import failure and run the stub tests from Tasks 1-3 instead.

- [ ] **Step 3: Commit**

```bash
git add tests/test_execute_command_runtime_smoke.py
git commit -m "test: cover execute_command thread task id smoke path"
```

---

### Task 5: Document Session Task ID Contract

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add runtime contract section**

Append this section to `README.md`:

```markdown
## Hermes Terminal Session Contract

The shell tool uses Hermes terminal toolkit under the hood. Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, the shell tool hashes that thread id into a path-safe Hermes `task_id`.
- The raw thread id is not exposed to the model and is not written into Hermes paths or checkpoints.
- If no runtime thread id is available, tools fall back to the Hermes `default` task id. This fallback is intended for local tests and direct function calls only.
- Production callers should provide a stable LangGraph `thread_id` for each conversation/run thread.

Future `terminal` and `process` tools must use the same helper in `agent_core.session_context` and must not expose `task_id` as a model-controlled argument.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document hermes session task id contract"
```

---

### Task 6: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run Python compilation checks**

Run:

```bash
python -m py_compile agent_core/session_context.py agent_tools/shell.py agent_tools/hermes_shell_adapter.py
```

Expected: command exits with code 0 and prints no syntax errors.

- [ ] **Step 2: Run focused unit tests**

Run:

```bash
python -m pytest tests/test_session_context.py tests/test_shell_task_id.py tests/test_hermes_shell_adapter_task_id.py -v
```

Expected: PASS. If `pytest` is unavailable, run `python -m pytest --version` and record the missing dependency.

- [ ] **Step 3: Run optional smoke tests**

Run:

```bash
python -m pytest tests/test_execute_command_runtime_smoke.py -v
```

Expected: PASS when LangChain/Pydantic are installed in the environment. If imports fail because the local environment lacks dependencies, record the exact missing module and rely on stub tests plus `py_compile`.

- [ ] **Step 4: Inspect changed files**

Run:

```bash
git diff -- agent_core/session_context.py agent_tools/shell.py agent_tools/hermes_shell_adapter.py README.md tests
```

Expected: Diff only contains the session task id helper, shell adapter wiring, tests, and README contract.

- [ ] **Step 5: Commit final verification updates if needed**

If Task 6 required any small fixes:

```bash
git add agent_core/session_context.py agent_tools/shell.py agent_tools/hermes_shell_adapter.py README.md tests
git commit -m "test: verify hermes thread task id integration"
```

If no files changed, do not create an empty commit.

---

## Self-Review

- Spec coverage: The plan maps LangGraph `thread_id` to Hermes `task_id`, hides it from the model, preserves fallback behavior, and prepares the same helper for future `terminal/process` wrappers.
- Placeholder scan: No task contains unresolved placeholders, unspecified validation, or references to undefined functions.
- Type consistency: `hermes_task_id_from_thread_id(thread_id: str | None) -> str`, `hermes_task_id_from_runtime(runtime: Any | None) -> str`, and `execute_command(command: str, runtime: ToolRuntime | None = None) -> str` are used consistently across tasks.
