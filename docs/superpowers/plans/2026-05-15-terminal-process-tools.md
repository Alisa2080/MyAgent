# Terminal And Process Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class `terminal` and `process` LangChain tools backed by `agent_tools/terminal_toolkit`, with LangGraph `ToolRuntime`-derived `task_id`, reference implementation guards, human approval, background process management, explicit session cleanup, and restart recovery.

**Architecture:** Keep `execute_command` as a safe compatibility entry point. Add project-native wrappers in `agent_tools/terminal_tools.py` instead of usingreference implementation `build_langchain_tools()`, because this project needs per-call `ToolRuntime` task id injection and process ownership checks. Normal agent turns preserve background processes; explicit user-session shutdown calls a lifecycle helper that kills processes for the session task id and cleans the terminal environment.

**Tech Stack:** Python 3.11, LangChain/LangGraph `ToolRuntime`, Pydantic v2, terminal toolkit vendored under `agent_tools/terminal_toolkit`, pytest.

---

## File Structure

- Create `agent_tools/terminal_tools.py`: LangChain `terminal` and `process` tools, input schemas without model-controlled `task_id`, runtime-derived task id, reference implementation guard usage, JSON response normalization, process session ownership checks.
- Create `agent_core/terminal_lifecycle.py`: startup recovery and explicit session shutdown helpers aroundreference implementation `process_registry` and `terminal_tool.cleanup_vm`.
- Modify `agent_core/delegation.py`: add `terminal` and `process` to parent-agent tools only; keep subagent read-only.
- Modify `agent_core/builders.py`: add human approval interception for `terminal` and process-mutating `process` calls.
- Modify `README.md`: document terminal/process contract, task id ownership, cleanup policy, and recovery policy.
- Test `tests/test_terminal_tools.py`: tool schemas, task id injection, reference implementation guard passthrough, background session ownership, process denial on cross-task sessions.
- Test `tests/test_terminal_lifecycle.py`: checkpoint recovery helper and explicit session cleanup helper.

## Policy Decisions

- `terminal` and `process` do not expose `task_id` to the model.
- `terminal` calls reference implementation with `force=False`, so reference implementation built-in command guards run.
- Human approval remains mandatory at LangChain middleware level for `terminal` and `process`.
- `execute_command` remains available for backward compatibility during this migration.
- Normal agent turn/session continuation preserves background processes.
- Explicit user-session shutdown calls `cleanup_terminal_session_for_runtime(runtime)` or `cleanup_terminal_session_for_thread_id(thread_id)`, which kills all process-registry entries for the derived task id and cleans the active terminal environment.
- Restart recovery is enabled by calling `recover_terminal_processes()` during agent construction. reference implementation can recover host PID sessions as detached sessions; sandbox PID sessions are skipped by existing reference implementation behavior.
- Subagents remain read-only and do not receive `terminal` or `process`.

## Task 1: Add Project-Native Terminal Tool Wrapper

**Files:**
- Create: `agent_tools/terminal_tools.py`
- Test: `tests/test_terminal_tools.py`

- [ ] **Step 1: Write the failing tests for terminal runtime task id and hidden schema**

Add this file:

```python
import json
from types import SimpleNamespace


def test_terminal_schema_does_not_expose_task_id():
    from agent_tools.terminal_tools import terminal

    assert "task_id" not in terminal.args
    assert "command" in terminal.args
    assert "background" in terminal.args
    assert "notify_on_complete" in terminal.args


def test_terminal_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import runtime_task_id_from_thread_id

    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="terminal-thread-1"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    raw = terminal_tools._terminal_impl(
        command="printf ok",
        background=False,
        timeout=30,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["data"]["output"] == "ok\n"
    assert calls[0]["task_id"] == runtime_task_id_from_thread_id("terminal-thread-1")
    assert calls[0]["force"] is False


def test_terminal_preserves_toolkit_guard_block_response(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools

    def fake_run_terminal(**kwargs):
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": "Command denied: destructive command",
                "status": "blocked",
            }
        )

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    raw = terminal_tools._terminal_impl(
        command="rm -rf tmp",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=None,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "blocked"
    assert payload["message"] == "Command denied: destructive command"
```

- [ ] **Step 2: Run tests to verify they fail before implementation**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py -v
```

Expected: FAIL because `agent_tools.terminal_tools` does not exist. If the command fails with `No module named pytest`, install pytest into the selected conda environment before continuing:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pip install pytest
```

- [ ] **Step 3: Implement the terminal wrapper**

Create `agent_tools/terminal_tools.py`:

```python
from __future__ import annotations

import json
from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.session_context import runtime_task_id_from_runtime
from agent_core.workspace import WORKDIR
from agent_tools.terminal_toolkit.terminal import run_process, run_terminal
from agent_tools.terminal_toolkit.process_registry import process_registry
from agent_tools.tool_output import tool_error, tool_ok


class TerminalInput(BaseModel):
    command: str = Field(description="Shell command to execute through terminal.")
    background: bool = Field(default=False, description="Run as a tracked background process.")
    timeout: int | None = Field(default=None, ge=1, description="Timeout in seconds.")
    workdir: str | None = Field(default=None, description="Optional per-command working directory.")
    pty: bool = Field(default=False, description="Use a PTY for interactive commands.")
    notify_on_complete: bool = Field(default=False, description="Queue completion notification for background commands.")
    watch_patterns: list[str] | None = Field(
        default=None,
        description="Output patterns that should trigger background process notifications.",
    )


class ProcessInput(BaseModel):
    action: Literal["list", "poll", "log", "wait", "kill", "write", "submit", "close"] = Field(
        description="Background process action."
    )
    session_id: str = Field(default="", description="Background process session id.")
    data: str = Field(default="", description="Data for write or submit actions.")
    timeout: int | None = Field(default=None, ge=1, description="Wait timeout in seconds.")
    offset: int = Field(default=0, ge=0, description="Log line offset.")
    limit: int = Field(default=200, ge=1, description="Maximum log lines to return.")


def _decode_terminal_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "terminal returned invalid JSON.", "raw": raw}
    if not isinstance(payload, dict):
        return {"error": f"terminal returned unexpected payload type: {type(payload).__name__}", "raw": raw}
    return payload


def _status_code_from_payload(payload: dict) -> str:
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    if payload.get("error"):
        return "terminal_error"
    return "terminal_error"


def _terminal_impl(
    *,
    command: str,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] | None = None,
    runtime: ToolRuntime | None = None,
) -> str:
    task_id = runtime_task_id_from_runtime(runtime)
    raw = run_terminal(
        command=command,
        background=background,
        timeout=timeout,
        task_id=task_id,
        workdir=workdir or str(WORKDIR),
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
        force=False,
    )
    payload = _decode_terminal_payload(raw)
    if payload.get("error"):
        return tool_error(
            "terminal",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "terminal_toolkit", "task_id": task_id},
        )
    return tool_ok(
        "terminal",
        data=payload,
        message="Terminal command completed." if not background else "Background process started.",
        meta={"backend": "terminal_toolkit", "task_id": task_id},
    )


@tool("terminal", args_schema=TerminalInput)
def terminal(
    command: str,
    runtime: ToolRuntime,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] | None = None,
) -> str:
    """Execute shell commands through terminal with runtime-scoped task isolation."""
    return _terminal_impl(
        command=command,
        background=background,
        timeout=timeout,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
        runtime=runtime,
    )
```

- [ ] **Step 4: Run terminal tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_terminal_schema_does_not_expose_task_id tests/test_terminal_tools.py::test_terminal_injects_runtime_thread_as_task_id tests/test_terminal_tools.py::test_terminal_preserves_toolkit_guard_block_response -v
```

Expected: PASS for the three terminal tests.

- [ ] **Step 5: Commit Task 1**

```bash
git add agent_tools/terminal_tools.py tests/test_terminal_tools.py
git commit -m "feat: add runtime-scoped terminal tool"
```

## Task 2: Add Process Tool With Task Ownership Enforcement

**Files:**
- Modify: `agent_tools/terminal_tools.py`
- Test: `tests/test_terminal_tools.py`

- [ ] **Step 1: Write failing tests for process schema and ownership checks**

Append to `tests/test_terminal_tools.py`:

```python
def test_process_schema_does_not_expose_task_id():
    from agent_tools.terminal_tools import process

    assert "task_id" not in process.args
    assert "action" in process.args
    assert "session_id" in process.args


def test_process_list_is_scoped_to_runtime_task_id(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import runtime_task_id_from_thread_id

    calls = []

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return json.dumps({"processes": []})

    monkeypatch.setattr(terminal_tools, "run_process", fake_run_process)
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="process-thread-1"))

    raw = terminal_tools._process_impl(
        action="list",
        session_id="",
        data="",
        timeout=None,
        offset=0,
        limit=200,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["data"]["processes"] == []
    assert calls[0]["task_id"] == runtime_task_id_from_thread_id("process-thread-1")


def test_process_rejects_cross_task_session(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools

    class FakeSession:
        task_id = "lg_other_task"

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: FakeSession())
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="current-thread"))

    raw = terminal_tools._process_impl(
        action="poll",
        session_id="proc_abc",
        data="",
        timeout=None,
        offset=0,
        limit=200,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "access_denied"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_process_schema_does_not_expose_task_id tests/test_terminal_tools.py::test_process_list_is_scoped_to_runtime_task_id tests/test_terminal_tools.py::test_process_rejects_cross_task_session -v
```

Expected: FAIL because `process` and `_process_impl` are not implemented.

- [ ] **Step 3: Implement process ownership checks and tool**

Append this implementation to `agent_tools/terminal_tools.py`:

```python
_PROCESS_ACTIONS_REQUIRING_SESSION = {"poll", "log", "wait", "kill", "write", "submit", "close"}


def _session_belongs_to_task(session_id: str, task_id: str) -> bool:
    session = process_registry.get(session_id)
    return bool(session is not None and session.task_id == task_id)


def _process_impl(
    *,
    action: str,
    session_id: str = "",
    data: str = "",
    timeout: int | None = None,
    offset: int = 0,
    limit: int = 200,
    runtime: ToolRuntime | None = None,
) -> str:
    task_id = runtime_task_id_from_runtime(runtime)
    if action in _PROCESS_ACTIONS_REQUIRING_SESSION:
        if not session_id:
            return tool_error("process", f"session_id is required for {action}", code="invalid_input")
        if not _session_belongs_to_task(session_id, task_id):
            return tool_error(
                "process",
                "Process session does not belong to the current runtime task id.",
                code="access_denied",
                data={"session_id": session_id},
                meta={"task_id": task_id},
            )

    raw = run_process(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
        task_id=task_id,
    )
    payload = _decode_terminal_payload(raw)
    if payload.get("error"):
        return tool_error(
            "process",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "terminal_toolkit", "task_id": task_id},
        )
    return tool_ok(
        "process",
        data=payload,
        message="Process action completed.",
        meta={"backend": "terminal_toolkit", "task_id": task_id},
    )


@tool("process", args_schema=ProcessInput)
def process(
    action: str,
    runtime: ToolRuntime,
    session_id: str = "",
    data: str = "",
    timeout: int | None = None,
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Manage reference implementation background processes scoped to the current runtime task id."""
    return _process_impl(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
        runtime=runtime,
    )
```

- [ ] **Step 4: Run process tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_process_schema_does_not_expose_task_id tests/test_terminal_tools.py::test_process_list_is_scoped_to_runtime_task_id tests/test_terminal_tools.py::test_process_rejects_cross_task_session -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add agent_tools/terminal_tools.py tests/test_terminal_tools.py
git commit -m "feat: add runtime-scoped terminal process tool"
```

## Task 3: Wire Tools Into Parent Agent And Human Approval

**Files:**
- Modify: `agent_core/delegation.py`
- Modify: `agent_core/builders.py`
- Test: `tests/test_terminal_tools.py`

- [ ] **Step 1: Write failing tests for tool registration and approval configuration**

Append to `tests/test_terminal_tools.py`:

```python
def _tool_names(tools):
    return {getattr(tool, "name", "") for tool in tools}


def test_parent_base_tools_include_terminal_and_process():
    from agent_core.delegation import BASE_TOOLS, READ_ONLY_TOOLS

    parent_names = _tool_names(BASE_TOOLS)
    read_only_names = _tool_names(READ_ONLY_TOOLS)

    assert "terminal" in parent_names
    assert "process" in parent_names
    assert "terminal" not in read_only_names
    assert "process" not in read_only_names


def test_human_interrupt_intercepts_terminal_and_process():
    from agent_core.builders import HUMAN_INTERRUPT_ON

    assert "terminal" in HUMAN_INTERRUPT_ON
    assert "process" in HUMAN_INTERRUPT_ON
    assert HUMAN_INTERRUPT_ON["terminal"]["allowed_decisions"] == ["approve", "edit", "reject", "respond"]
    assert HUMAN_INTERRUPT_ON["process"]["allowed_decisions"] == ["approve", "edit", "reject", "respond"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_parent_base_tools_include_terminal_and_process tests/test_terminal_tools.py::test_human_interrupt_intercepts_terminal_and_process -v
```

Expected: FAIL because parent tools and interrupt config do not include `terminal` and `process`.

- [ ] **Step 3: Add tools to parent agent only**

Modify `agent_core/delegation.py` imports:

```python
from agent_tools.terminal_tools import process, terminal
```

Modify `BASE_TOOLS`:

```python
BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    write_file,
    patch,
    execute_command,
    terminal,
    process,
    skill_manage,
]
```

Do not modify `READ_ONLY_TOOLS`; subagents stay read-only.

- [ ] **Step 4: Add human approval entries**

Modify `HUMAN_INTERRUPT_ON` in `agent_core/builders.py`:

```python
    "terminal": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this terminal command before it executes.",
    },
    "process": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this reference implementation background process action before it executes.",
    },
```

Place these next to the existing `execute_command` entry.

- [ ] **Step 5: Run registration tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_parent_base_tools_include_terminal_and_process tests/test_terminal_tools.py::test_human_interrupt_intercepts_terminal_and_process -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add agent_core/delegation.py agent_core/builders.py tests/test_terminal_tools.py
git commit -m "feat: expose terminal tools to parent agent"
```

## Task 4: Add Terminal Lifecycle Recovery And Explicit Cleanup

**Files:**
- Create: `agent_core/terminal_lifecycle.py`
- Modify: `agent_core/builders.py`
- Test: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Write failing lifecycle tests**

Create `tests/test_terminal_lifecycle.py`:

```python
from types import SimpleNamespace

from agent_core.session_context import runtime_task_id_from_thread_id


def test_recover_terminal_processes_delegates_to_registry(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []

    def fake_recover():
        calls.append("recover")
        return 2

    monkeypatch.setattr(lifecycle.process_registry, "recover_from_checkpoint", fake_recover)

    assert lifecycle.recover_terminal_processes() == 2
    assert calls == ["recover"]


def test_cleanup_terminal_session_for_thread_id_kills_processes_and_cleans_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = runtime_task_id_from_thread_id("cleanup-thread")
    killed = []
    cleaned = []

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda task_id=None: killed.append(task_id) or 3)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    result = lifecycle.cleanup_terminal_session_for_thread_id("cleanup-thread")

    assert result == {"task_id": task_id, "killed_processes": 3, "environment_cleaned": True}
    assert killed == [task_id]
    assert cleaned == [task_id]


def test_cleanup_terminal_session_for_runtime_uses_runtime_thread(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = runtime_task_id_from_thread_id("runtime-cleanup-thread")
    killed = []
    cleaned = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="runtime-cleanup-thread"))

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda task_id=None: killed.append(task_id) or 1)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    result = lifecycle.cleanup_terminal_session_for_runtime(runtime)

    assert result["task_id"] == task_id
    assert result["killed_processes"] == 1
    assert killed == [task_id]
    assert cleaned == [task_id]
```

- [ ] **Step 2: Run lifecycle tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -v
```

Expected: FAIL because `agent_core.terminal_lifecycle` does not exist.

- [ ] **Step 3: Implement lifecycle helpers**

Create `agent_core/terminal_lifecycle.py`:

```python
from __future__ import annotations

import logging
from typing import Any

from agent_core.session_context import runtime_task_id_from_runtime, runtime_task_id_from_thread_id
from agent_tools.terminal_toolkit.process_registry import process_registry
from agent_tools.terminal_toolkit.terminal_tool import cleanup_vm

logger = logging.getLogger(__name__)


def recover_terminal_processes() -> int:
    """Recover host-backed reference implementation background processes from checkpoint metadata."""
    recovered = process_registry.recover_from_checkpoint()
    logger.info("Recovered %s terminal process(es) from checkpoint.", recovered)
    return recovered


def cleanup_terminal_session_for_thread_id(thread_id: str | None) -> dict:
    """Explicitly end a terminal session: kill scoped processes and clean environment."""
    task_id = runtime_task_id_from_thread_id(thread_id)
    killed = process_registry.kill_all(task_id=task_id)
    cleanup_vm(task_id)
    return {
        "task_id": task_id,
        "killed_processes": killed,
        "environment_cleaned": True,
    }


def cleanup_terminal_session_for_runtime(runtime: Any | None) -> dict:
    """Explicitly end the terminal session associated with a ToolRuntime-like object."""
    task_id = runtime_task_id_from_runtime(runtime)
    killed = process_registry.kill_all(task_id=task_id)
    cleanup_vm(task_id)
    return {
        "task_id": task_id,
        "killed_processes": killed,
        "environment_cleaned": True,
    }
```

- [ ] **Step 4: Call recovery during parent agent construction**

Modify `agent_core/builders.py` imports:

```python
from agent_core.terminal_lifecycle import recover_terminal_processes
```

Modify `build_agent()` immediately after `memory_store.load_from_disk()`:

```python
    recover_terminal_processes()
```

- [ ] **Step 5: Run lifecycle tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add agent_core/terminal_lifecycle.py agent_core/builders.py tests/test_terminal_lifecycle.py
git commit -m "feat: add terminal lifecycle helpers"
```

## Task 5: Add Real LangGraph Injection Smoke Test

**Files:**
- Modify: `tests/test_terminal_tools.py`

- [ ] **Step 1: Add a real ToolNode smoke test for terminal**

Append to `tests/test_terminal_tools.py`:

```python
def test_terminal_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import runtime_task_id_from_thread_id
    from agent_tools.terminal_tools import terminal

    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "toolnode-ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([terminal]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "terminal",
                            "args": {"command": "printf toolnode-ok"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-terminal-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == runtime_task_id_from_thread_id("toolnode-terminal-thread")
```

- [ ] **Step 2: Run smoke test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py::test_terminal_toolnode_injects_runtime_thread -v
```

Expected: PASS.

- [ ] **Step 3: Commit Task 5**

```bash
git add tests/test_terminal_tools.py
git commit -m "test: cover terminal ToolNode runtime injection"
```

## Task 6: Document Terminal/Process Contract

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add documentation**

Replace the existing `## Terminal Session Contract` section in `README.md` with:

```markdown
## Terminal Session Contract

The project exposes three shell-related tools:

- `execute_command`: compatibility tool for foreground commands with the legacy output schema and stricter project-side blocking.
- `terminal`: first-class terminal tool for foreground and background commands.
- `process`: first-class terminal process tool for background process polling, logs, waiting, stdin, and killing.

Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, terminal tools hash that thread id into a path-safereference implementation `task_id`.
- If `execution_info.thread_id` is unavailable, tools fall back to `runtime.config["configurable"]["thread_id"]`.
- The raw thread id is not exposed to the model and is not written into reference implementation paths or checkpoints.
- If no runtime thread id is available, tools fall back to thereference implementation `default` task id. This fallback is intended for local tests and direct implementation calls only.
- Production callers should provide a stable LangGraph `thread_id` for each user conversation/session.

`terminal` and `process` do not expose `task_id` in their tool schemas. `process` validates that `session_id` belongs to the current runtime-derived `task_id` before allowing `poll`, `log`, `wait`, `kill`, `write`, `submit`, or `close`.

Security and approval:

- `terminal` uses reference implementation built-in command guards by calling reference implementation with `force=False`.
- `terminal` and `process` are intercepted by the human-in-the-loop middleware before execution.
- `execute_command` remains as a compatibility layer and should not be used for new long-running/background workflows.

Lifecycle policy:

- Normal agent turns preserve background processes.
- Explicit user-session shutdown should call `cleanup_terminal_session_for_thread_id(thread_id)` or `cleanup_terminal_session_for_runtime(runtime)`.
- Explicit cleanup kills running processes for the session task id and cleans the active terminal environment.
- Parent agent startup calls `recover_terminal_processes()`. reference implementation can recover host-backed background processes as detached sessions after restart; sandbox-backed processes are skipped by reference implementation because their in-sandbox PIDs are not meaningful after restart.
```

- [ ] **Step 2: Run documentation-adjacent checks**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 3: Commit Task 6**

```bash
git add README.md
git commit -m "docs: document terminal lifecycle contract"
```

## Task 7: Full Verification

**Files:**
- Verify: all modified files

- [ ] **Step 1: Run focused test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_execute_command_runtime_smoke.py tests/test_shell_task_id.py tests/test_session_context.py tests/test_shell_adapter_task_id.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run compile check**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_tools/terminal_tools.py agent_core/terminal_lifecycle.py agent_core/builders.py agent_core/delegation.py agent_tools/shell.py agent_core/session_context.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff --stat origin/main..HEAD
git diff origin/main..HEAD -- agent_tools/terminal_tools.py agent_core/terminal_lifecycle.py agent_core/delegation.py agent_core/builders.py README.md tests/test_terminal_tools.py tests/test_terminal_lifecycle.py
```

Expected: diff contains only terminal/process tool wiring, lifecycle helpers, docs, and tests.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` with:

```text
DESCRIPTION: Added first-class terminal/process tools with runtime-derived task_id, process ownership checks, human approval wiring, lifecycle cleanup, and restart recovery.
PLAN_OR_REQUIREMENTS: docs/superpowers/plans/2026-05-15-archived_reference-terminal-process-tools.md
BASE_SHA: origin/main
HEAD_SHA: HEAD
```

Expected: reviewer returns no Critical issues before merge or release.

## Self-Review

Spec coverage:

- First-class `terminal` and `process`: Task 1, Task 2, Task 3.
- `runtime: ToolRuntime` injection: Task 1, Task 2, Task 5.
- Use reference implementation guard: Task 1 passes `force=False`.
- Use human approval: Task 3 adds middleware entries.
- Decide cleanup policy: Policy section and Task 4 implement explicit cleanup.
- Preserve background processes: Policy section documents preservation during normal turns.
- Allow restart recovery: Task 4 calls `recover_terminal_processes()` during parent agent construction.
- Keep `execute_command` compatibility: Policy section and Task 3 keep it in `BASE_TOOLS`.

Placeholder scan:

- No unspecified implementation steps remain.
- All new functions, imports, tests, commands, and commit commands are concrete.

Type consistency:

- `terminal(command, runtime, ...)` and `process(action, runtime, ...)` use required `ToolRuntime` so LangGraph `ToolNode` injects runtime.
- Internal implementations use optional runtime for direct unit tests only.
- Task id derivation consistently uses `runtime_task_id_from_runtime()`.
