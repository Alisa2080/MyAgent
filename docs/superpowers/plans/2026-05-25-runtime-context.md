# RuntimeContext Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `RuntimeContext` identity abstraction and migrate the duplicated thread/task/tool-call extraction in public tool wrappers without changing behavior.

**Architecture:** `agent_core.session_context` remains the single identity module. A frozen `RuntimeContext` dataclass centralizes extraction from LangChain/ToolRuntime-like objects while preserving existing public helper functions. Tool wrappers consume the context object but keep policy, approval, Hermes, cron, and result formatting logic unchanged.

**Tech Stack:** Python 3.11, dataclasses, pytest, LangChain `ToolRuntime`, LangGraph ToolNode tests, existing Hermes terminal/file tool wrappers.

---

## File Structure

- Modify `agent_core/session_context.py`: define `RuntimeContext`, thread source literals, and compatibility helper delegation.
- Modify `tests/test_session_context.py`: add focused tests for `RuntimeContext` behavior and preserve existing helper expectations.
- Modify `agent_tools/public/terminal.py`: use `RuntimeContext.from_runtime(runtime)` in terminal and process implementations.
- Modify `agent_tools/public/files.py`: use `RuntimeContext.from_runtime(runtime)` for task ids and approval tool call ids.
- Modify `agent_tools/public/cronjob.py`: use `RuntimeContext.from_runtime(runtime).thread_id` for origin metadata.
- No new runtime modules. No changes to `agent_core/agent_runner.py` in this implementation.

Use this interpreter for all Python and pytest commands:

```bash
/home/miku/miniforge3/envs/langchain/bin/python
```

Baseline already verified in this worktree:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_session_context.py tests/test_terminal_tools.py tests/test_file_tools_runtime_task_id.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_file_wrappers.py tests/test_cronjob_tool.py -q
```

Expected baseline result:

```text
93 passed
```

---

### Task 1: Add Failing RuntimeContext Tests

**Files:**
- Modify: `tests/test_session_context.py`

- [ ] **Step 1: Update imports in `tests/test_session_context.py`**

Replace the current import block:

```python
from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_runtime, hermes_task_id_from_thread_id
```

with:

```python
from types import SimpleNamespace

from agent_core.session_context import (
    RuntimeContext,
    hermes_task_id_from_runtime,
    hermes_task_id_from_thread_id,
)
```

- [ ] **Step 2: Add tests after `test_runtime_config_thread_id_is_fallback_when_execution_info_missing`**

Append these tests:

```python
def test_runtime_context_prefers_execution_info_thread_id():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
        tool_call_id="call-123",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id == "thread-from-runtime"
    assert ctx.task_id == hermes_task_id_from_thread_id("thread-from-runtime")
    assert ctx.tool_call_id == "call-123"
    assert ctx.thread_source == "execution_info"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_falls_back_to_config_thread_id():
    runtime = SimpleNamespace(
        execution_info=None,
        config={"configurable": {"thread_id": "thread-from-config"}},
        tool_call_id="call-456",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id == "thread-from-config"
    assert ctx.task_id == hermes_task_id_from_thread_id("thread-from-config")
    assert ctx.tool_call_id == "call-456"
    assert ctx.thread_source == "config"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_falls_back_to_default_without_runtime():
    ctx = RuntimeContext.from_runtime(None)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is False
    assert ctx.is_default_task is True


def test_runtime_context_treats_empty_thread_ids_as_missing():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=""),
        config={"configurable": {"thread_id": ""}},
        tool_call_id="",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is False
    assert ctx.is_default_task is True


def test_runtime_context_ignores_non_dict_config():
    runtime = SimpleNamespace(
        execution_info=None,
        config="not-a-dict",
        tool_call_id=None,
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"


def test_runtime_context_from_thread_id():
    ctx = RuntimeContext.from_thread_id("manual-thread")

    assert ctx.thread_id == "manual-thread"
    assert ctx.task_id == hermes_task_id_from_thread_id("manual-thread")
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_session_context.py -q
```

Expected: FAIL with an import error like:

```text
ImportError: cannot import name 'RuntimeContext'
```

- [ ] **Step 4: Commit failing tests**

Run:

```bash
git add tests/test_session_context.py
git commit -m "test: cover runtime context identity extraction"
```

---

### Task 2: Implement RuntimeContext

**Files:**
- Modify: `agent_core/session_context.py`
- Test: `tests/test_session_context.py`

- [ ] **Step 1: Replace `agent_core/session_context.py` imports**

Change:

```python
from hashlib import sha256
from typing import Any
```

to:

```python
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal
```

- [ ] **Step 2: Add `ThreadSource` and `RuntimeContext` after constants**

Insert this block after `_FALLBACK_TASK_ID = "default"`:

```python
ThreadSource = Literal["execution_info", "config", "fallback"]


@dataclass(frozen=True)
class RuntimeContext:
    """Resolved LangGraph/LangChain runtime identity for project tools."""

    thread_id: str | None
    task_id: str
    tool_call_id: str | None = None
    thread_source: ThreadSource = "fallback"

    @classmethod
    def from_runtime(cls, runtime: Any | None) -> "RuntimeContext":
        thread_id = _thread_id_from_execution_info(runtime)
        if thread_id:
            return cls(
                thread_id=thread_id,
                task_id=hermes_task_id_from_thread_id(thread_id),
                tool_call_id=_tool_call_id_from_runtime(runtime),
                thread_source="execution_info",
            )

        thread_id = _thread_id_from_config(runtime)
        if thread_id:
            return cls(
                thread_id=thread_id,
                task_id=hermes_task_id_from_thread_id(thread_id),
                tool_call_id=_tool_call_id_from_runtime(runtime),
                thread_source="config",
            )

        return cls(
            thread_id=None,
            task_id=_FALLBACK_TASK_ID,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            thread_source="fallback",
        )

    @classmethod
    def from_thread_id(cls, thread_id: str | None) -> "RuntimeContext":
        normalized = str(thread_id) if thread_id else None
        return cls(
            thread_id=normalized,
            task_id=hermes_task_id_from_thread_id(normalized),
            thread_source="fallback",
        )

    @property
    def has_thread(self) -> bool:
        return self.thread_id is not None

    @property
    def is_default_task(self) -> bool:
        return self.task_id == _FALLBACK_TASK_ID
```

- [ ] **Step 3: Update `hermes_task_id_from_runtime` to delegate**

Replace:

```python
def hermes_task_id_from_runtime(runtime: Any | None) -> str:
    """Extract LangGraph thread identity from ToolRuntime-like objects."""
    thread_id = _thread_id_from_execution_info(runtime)
    if thread_id:
        return hermes_task_id_from_thread_id(thread_id)

    thread_id = _thread_id_from_config(runtime)
    return hermes_task_id_from_thread_id(thread_id)
```

with:

```python
def hermes_task_id_from_runtime(runtime: Any | None) -> str:
    """Extract LangGraph thread identity from ToolRuntime-like objects."""
    return RuntimeContext.from_runtime(runtime).task_id
```

- [ ] **Step 4: Add `_tool_call_id_from_runtime` at the end of the helper section**

Append this helper after `_thread_id_from_config`:

```python
def _tool_call_id_from_runtime(runtime: Any | None) -> str | None:
    value = getattr(runtime, "tool_call_id", None)
    return str(value) if value else None
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_session_context.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit implementation**

Run:

```bash
git add agent_core/session_context.py tests/test_session_context.py
git commit -m "feat: add runtime context identity abstraction"
```

---

### Task 3: Migrate Terminal And Process Wrappers

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Test: `tests/test_terminal_tools.py`
- Test: `tests/test_permissions_terminal_wrappers.py`
- Test: `tests/test_permissions_process_wrappers.py`

- [ ] **Step 1: Update imports in `agent_tools/public/terminal.py`**

Replace:

```python
from agent_core.session_context import hermes_task_id_from_runtime
```

with:

```python
from agent_core.session_context import RuntimeContext
```

- [ ] **Step 2: Remove the local `_tool_call_id_from_runtime` helper**

Delete this function:

```python
def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    value = getattr(runtime, "tool_call_id", None)
    return str(value) if value else None
```

- [ ] **Step 3: Update `_terminal_impl` identity extraction**

Replace:

```python
    task_id = hermes_task_id_from_runtime(runtime)
    tool_call_id = _tool_call_id_from_runtime(runtime)
```

with:

```python
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id
    tool_call_id = runtime_context.tool_call_id
```

- [ ] **Step 4: Update `_process_impl` identity extraction**

Replace:

```python
    task_id = hermes_task_id_from_runtime(runtime)
    tool_call_id = _tool_call_id_from_runtime(runtime)
```

with:

```python
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id
    tool_call_id = runtime_context.tool_call_id
```

- [ ] **Step 5: Run terminal/process tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit terminal migration**

Run:

```bash
git add agent_tools/public/terminal.py
git commit -m "refactor: use runtime context in terminal tools"
```

---

### Task 4: Migrate File Tool Wrappers

**Files:**
- Modify: `agent_tools/public/files.py`
- Test: `tests/test_file_tools_runtime_task_id.py`
- Test: `tests/test_permissions_file_wrappers.py`

- [ ] **Step 1: Update imports in `agent_tools/public/files.py`**

Replace:

```python
from agent_core.session_context import hermes_task_id_from_runtime
```

with:

```python
from agent_core.session_context import RuntimeContext
```

- [ ] **Step 2: Update `_task_id_from_runtime`**

Replace:

```python
def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return hermes_task_id_from_runtime(runtime)
```

with:

```python
def _runtime_context(runtime: ToolRuntime | None) -> RuntimeContext:
    return RuntimeContext.from_runtime(runtime)


def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return _runtime_context(runtime).task_id
```

- [ ] **Step 3: Update `_tool_call_id_from_runtime`**

Replace:

```python
def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    tool_call_id = getattr(runtime, "tool_call_id", None)
    return str(tool_call_id) if tool_call_id else None
```

with:

```python
def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    return _runtime_context(runtime).tool_call_id
```

- [ ] **Step 4: Run file wrapper tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_tools_runtime_task_id.py tests/test_permissions_file_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit file migration**

Run:

```bash
git add agent_tools/public/files.py
git commit -m "refactor: use runtime context in file tools"
```

---

### Task 5: Migrate Cron Tool Origin Thread Extraction

**Files:**
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Add RuntimeContext import**

After:

```python
from pydantic import BaseModel, Field
```

add:

```python
from agent_core.session_context import RuntimeContext
```

- [ ] **Step 2: Replace `_runtime_thread_id`**

Replace:

```python
def _runtime_thread_id(runtime: ToolRuntime | None) -> str | None:
    config = getattr(runtime, "config", None)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    value = configurable.get("thread_id") if isinstance(configurable, dict) else None
    return str(value) if value else None
```

with:

```python
def _runtime_thread_id(runtime: ToolRuntime | None) -> str | None:
    return RuntimeContext.from_runtime(runtime).thread_id
```

- [ ] **Step 3: Run cron tool tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cronjob_tool.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit cron migration**

Run:

```bash
git add agent_tools/public/cronjob.py
git commit -m "refactor: use runtime context in cron tool"
```

---

### Task 6: Final Verification And Cleanup

**Files:**
- Verify: `agent_core/session_context.py`
- Verify: `agent_tools/public/terminal.py`
- Verify: `agent_tools/public/files.py`
- Verify: `agent_tools/public/cronjob.py`
- Verify: `tests/test_session_context.py`

- [ ] **Step 1: Search for old duplicate runtime helpers**

Run:

```bash
rg -n "getattr\\(runtime, \\\"tool_call_id\\\"|def _runtime_thread_id|hermes_task_id_from_runtime\\(runtime\\)" agent_core agent_tools
```

Expected remaining matches:

```text
agent_core/human_loop.py
agent_core/terminal_lifecycle.py
```

`agent_core/human_loop.py` and `agent_core/terminal_lifecycle.py` can remain because they are outside the first implementation scope and use existing compatibility helpers or tool call ids from `ToolCall` payloads.

- [ ] **Step 2: Run focused regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_session_context.py tests/test_terminal_tools.py tests/test_file_tools_runtime_task_id.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py tests/test_permissions_file_wrappers.py tests/test_cronjob_tool.py -q
```

Expected: PASS.

- [ ] **Step 3: Run compile check**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_core/session_context.py agent_tools/public/terminal.py agent_tools/public/files.py agent_tools/public/cronjob.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected:

```text

```

The worktree should be clean because each implementation task commits its changes.

- [ ] **Step 5: Report results**

Report:

```text
Branch: runtime-context
Worktree: /home/miku/projects/langchain/.worktrees/runtime-context
Focused tests: PASS
Compile check: PASS
Commits: <list commits from this plan>
```
