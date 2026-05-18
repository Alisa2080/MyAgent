# Hermes Background Process Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the Hermes-backed `terminal(background=True)` and `process(...)` integration with per-`task_id` background process quotas, automatic session cleanup hooks, and new-user-message interrupt wiring for blocking `process(action="wait")` calls.

**Architecture:** Keep the existing project-native LangChain wrappers in `agent_tools/terminal_tools.py`. Add project-level governance around Hermes rather than modifying model-visible tool schemas. Runtime `task_id` remains derived from LangGraph `thread_id` and hidden from the model. Background process quotas are enforced before starting a new `terminal(background=True)` process. Session cleanup and interrupt behavior are exposed as explicit, small lifecycle APIs that the LangGraph/gateway layer can call when a user session ends or a new user message arrives.

**Tech Stack:** Python 3.11, LangChain/LangGraph `ToolRuntime`, vendored Hermes terminal toolkit, pytest.

---

## Current State

- `terminal(background=True)` starts tracked background processes through Hermes `process_registry`.
- `process(action="poll" | "log" | "wait" | "kill" | "write" | "submit" | "close")` manages those sessions and validates that `session_id` belongs to the current runtime-derived `task_id`.
- Hermes has `MAX_PROCESSES = 64`, but this is a registry pruning threshold, not a strict per-session running-process quota.
- `process_registry.wait()` checks Hermes interrupt state and can return `status="interrupted"`, but the project does not yet wire “new user message arrived” to Hermes `set_interrupt(...)`.
- `cleanup_terminal_session_for_thread_id()` and `cleanup_terminal_session_for_runtime()` exist, but they are explicit helpers only; there is no project-level automatic session-end hook.

## Policy Decisions

- Enforce a **per-`task_id` running background process quota** at the LangChain wrapper layer before calling Hermes `run_terminal(background=True)`.
- Default quota: `3` running background processes per `task_id`.
- Configure quota with `HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK`; invalid values fall back to `3`.
- Count only live/running sessions for that `task_id`. Finished sessions should not consume quota.
- Return a structured `tool_error("terminal", ..., code="background_quota_exceeded")` when quota is exceeded.
- Keep global Hermes registry pruning unchanged.
- Keep `terminal` and `process` human approval behavior unchanged.
- Keep normal turns preserving background processes.
- Add an explicit automatic-cleanup integration function that callers can bind to their session-end event.
- Default session-end policy: **kill on explicit session close**, not on every agent turn.
- Add new-message interrupt APIs that are callable by the gateway before starting a replacement run for the same user session.
- Do not attempt to infer user-message arrival inside `process(wait)` itself; the event must come from the caller/gateway because the blocking wait loop cannot observe LangGraph input events directly.
- Keep long-lived servers allowed, bounded by quota and explicit cleanup policy.

## File Structure

- Modify `agent_tools/terminal_tools.py`: enforce per-`task_id` background quota before starting a background process.
- Create `agent_core/terminal_process_policy.py`: quota configuration and live-process counting helpers.
- Modify `agent_core/terminal_lifecycle.py`: add session-end and new-message interrupt lifecycle APIs.
- Create `tests/test_terminal_process_policy.py`: quota configuration and counting behavior.
- Extend `tests/test_terminal_tools.py`: quota pass/fail behavior for `terminal(background=True)`.
- Extend `tests/test_terminal_lifecycle.py`: automatic cleanup hook and interrupt wiring behavior.
- Modify `README.md`: document quotas, cleanup integration points, interrupt integration points, and long-lived server policy.

---

## Task 1: Add Per-task Background Process Quota Policy

**Files:**
- Create: `agent_core/terminal_process_policy.py`
- Test: `tests/test_terminal_process_policy.py`

- [ ] **Step 1: Write quota policy tests**

Add tests for:

- default quota is `3`;
- `HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK=5` returns `5`;
- invalid, zero, or negative env values fall back to default;
- live process count includes only sessions whose `task_id` matches and `exited is False`;
- finished sessions do not consume quota.

Suggested test shape:

```python
from types import SimpleNamespace


def test_default_background_quota(monkeypatch):
    from agent_core import terminal_process_policy as policy

    monkeypatch.delenv("HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK", raising=False)

    assert policy.max_background_processes_per_task() == 3


def test_count_running_background_processes_for_task(monkeypatch):
    from agent_core import terminal_process_policy as policy

    sessions = [
        SimpleNamespace(task_id="task-a", exited=False),
        SimpleNamespace(task_id="task-a", exited=True),
        SimpleNamespace(task_id="task-b", exited=False),
    ]

    class FakeRegistry:
        _running = {f"proc-{idx}": session for idx, session in enumerate(sessions)}
        _lock = None

    assert policy.count_running_background_processes("task-a", registry=FakeRegistry()) == 1
```

- [ ] **Step 2: Implement policy helpers**

Create `agent_core/terminal_process_policy.py`:

```python
from __future__ import annotations

import os
from typing import Any

from agent_tools.hermes_terminal_toolkit.process_registry import process_registry

DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK = 3
MAX_BACKGROUND_PROCESSES_ENV = "HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK"


def max_background_processes_per_task() -> int:
    raw = os.getenv(MAX_BACKGROUND_PROCESSES_ENV)
    try:
        value = int(raw) if raw is not None else DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    if value < 1:
        return DEFAULT_MAX_BACKGROUND_PROCESSES_PER_TASK
    return value


def count_running_background_processes(task_id: str, registry: Any = process_registry) -> int:
    lock = getattr(registry, "_lock", None)
    running = getattr(registry, "_running", {})
    if lock is None:
        sessions = list(running.values())
    else:
        with lock:
            sessions = list(running.values())
    return sum(1 for session in sessions if session.task_id == task_id and not session.exited)


def background_quota_available(task_id: str, registry: Any = process_registry) -> tuple[bool, int, int]:
    current = count_running_background_processes(task_id, registry=registry)
    limit = max_background_processes_per_task()
    return current < limit, current, limit
```

- [ ] **Step 3: Run policy tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_process_policy.py -v
```

Expected: tests pass.

---

## Task 2: Enforce Quota in `terminal(background=True)`

**Files:**
- Modify: `agent_tools/terminal_tools.py`
- Test: `tests/test_terminal_tools.py`

- [ ] **Step 1: Write failing terminal quota tests**

Add tests for:

- foreground commands do not check quota;
- background command proceeds when current running count is below limit;
- background command returns `background_quota_exceeded` when count is at limit;
- quota failure does not call `run_terminal`.

Suggested assertions:

```python
def test_terminal_background_rejects_when_task_quota_exceeded(monkeypatch):
    import json
    import agent_tools.terminal_tools as terminal_tools

    calls = []
    monkeypatch.setattr(terminal_tools, "background_quota_available", lambda task_id: (False, 3, 3))
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    raw = terminal_tools._terminal_impl(command="python -m http.server", background=True, runtime=None)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "background_quota_exceeded"
    assert payload["data"]["current"] == 3
    assert payload["data"]["limit"] == 3
    assert calls == []
```

- [ ] **Step 2: Implement quota check**

In `agent_tools/terminal_tools.py`:

- import `background_quota_available`;
- after deriving `task_id`, before calling `run_terminal`, check quota only when `background is True`;
- return structured error if quota is exceeded.

Expected behavior:

```python
if background:
    available, current, limit = background_quota_available(task_id)
    if not available:
        return tool_error(
            "terminal",
            f"Background process quota exceeded for this session ({current}/{limit}).",
            code="background_quota_exceeded",
            data={"current": current, "limit": limit},
            meta={"backend": "hermes_terminal_toolkit"},
        )
```

- [ ] **Step 3: Run terminal tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_terminal_process_policy.py -v
```

Expected: tests pass.

---

## Task 3: Add Session-end Auto-cleanup API

**Files:**
- Modify: `agent_core/terminal_lifecycle.py`
- Test: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Write cleanup API tests**

Add tests for:

- `end_terminal_session(thread_id)` delegates to `cleanup_terminal_session_for_thread_id(thread_id)`;
- missing `thread_id` does not accidentally kill shared/default state unless explicitly allowed;
- returned payload includes `cleanup_reason`;
- cleanup is idempotent from the caller perspective if there are no running processes.

Policy detail:

- Avoid calling cleanup with `thread_id=None` in automatic session-end paths.
- Direct test/local fallback to `default` should remain available only through existing explicit helpers.
- New auto-cleanup API should reject missing `thread_id` with `cleaned=False`.

- [ ] **Step 2: Implement auto-cleanup lifecycle function**

Add to `agent_core/terminal_lifecycle.py`:

```python
def end_terminal_session(thread_id: str | None, *, reason: str = "session_closed") -> dict:
    """Automatic session-end hook for callers that know the LangGraph thread id."""
    if not thread_id:
        return {
            "cleaned": False,
            "cleanup_reason": reason,
            "error": "thread_id is required for automatic terminal session cleanup",
        }
    result = cleanup_terminal_session_for_thread_id(thread_id)
    return {
        **result,
        "cleaned": True,
        "cleanup_reason": reason,
    }
```

- [ ] **Step 3: Decide call-site integration**

There is currently no gateway/session manager in this repository. Therefore implementation should expose `end_terminal_session(...)` as the stable integration point and document where it must be called:

- user explicitly closes a conversation;
- gateway deletes a LangGraph thread;
- user logs out and the product wants to release terminal resources;
- server shutdown wants best-effort cleanup for selected sessions.

Do not call this from `build_agent()` or every run, because that would kill intended long-lived background processes between normal turns.

- [ ] **Step 4: Run lifecycle tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -v
```

Expected: tests pass.

---

## Task 4: Add New-user-message Interrupt Wiring API

**Files:**
- Modify: `agent_core/terminal_lifecycle.py`
- Test: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Write interrupt wiring tests**

Add tests for:

- registering a running agent execution thread for a LangGraph `thread_id`;
- interrupting a registered thread calls Hermes `set_interrupt(True, thread_id=<python-thread-ident>)`;
- clearing an execution registration calls `set_interrupt(False, thread_id=<python-thread-ident>)`;
- interrupting an unknown `thread_id` returns `interrupted=False`;
- thread registration is safe under a lock.

The Hermes interrupt API is thread-ident based, while this project’s session identity is LangGraph `thread_id`. The missing bridge is a registry:

```text
LangGraph thread_id -> active Python execution thread ident
```

- [ ] **Step 2: Implement active execution registry**

Add to `agent_core/terminal_lifecycle.py`:

```python
import threading
from contextlib import contextmanager

from agent_tools.hermes_terminal_toolkit.interrupt import set_interrupt

_active_execution_threads: dict[str, int] = {}
_active_execution_lock = threading.Lock()


@contextmanager
def terminal_execution_scope(thread_id: str | None):
    """Register the current Python thread as the active execution for a LangGraph thread."""
    if not thread_id:
        yield
        return
    python_thread_id = threading.current_thread().ident
    with _active_execution_lock:
        if python_thread_id is not None:
            _active_execution_threads[str(thread_id)] = python_thread_id
            set_interrupt(False, thread_id=python_thread_id)
    try:
        yield
    finally:
        with _active_execution_lock:
            registered = _active_execution_threads.get(str(thread_id))
            if registered == python_thread_id:
                del _active_execution_threads[str(thread_id)]
            if python_thread_id is not None:
                set_interrupt(False, thread_id=python_thread_id)


def interrupt_terminal_wait_for_thread_id(thread_id: str | None, *, reason: str = "new_user_message") -> dict:
    """Signal a blocking terminal/process wait for this LangGraph thread to return early."""
    if not thread_id:
        return {"interrupted": False, "reason": reason, "error": "thread_id is required"}
    with _active_execution_lock:
        python_thread_id = _active_execution_threads.get(str(thread_id))
    if python_thread_id is None:
        return {"interrupted": False, "reason": reason}
    set_interrupt(True, thread_id=python_thread_id)
    return {"interrupted": True, "reason": reason, "python_thread_id": python_thread_id}
```

- [ ] **Step 3: Integrate execution scope where agent runs are invoked**

Current repository only exposes `agent = build_agent()` in `agent.py`; it does not include the gateway code that receives user messages or invokes the agent per session. Therefore add a small helper that callers can use:

```python
def invoke_with_terminal_lifecycle(agent, input_data, config):
    thread_id = (((config or {}).get("configurable") or {}).get("thread_id"))
    with terminal_execution_scope(thread_id):
        return agent.invoke(input_data, config)
```

Only add this helper if there is an appropriate project module for runtime/session helpers. If no such module exists, keep the API in `terminal_lifecycle.py` and document external integration.

- [ ] **Step 4: Gateway integration contract**

The gateway or caller must do this:

1. Before starting an agent run for `thread_id`, wrap the run in `terminal_execution_scope(thread_id)`.
2. When a new user message arrives for the same `thread_id` while an earlier run is still waiting, call `interrupt_terminal_wait_for_thread_id(thread_id)`.
3. Then start/resume the new run normally.
4. `process(action="wait")` will detect Hermes interrupt and return `status="interrupted"` within roughly one second.

- [ ] **Step 5: Run lifecycle tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -v
```

Expected: tests pass.

---

## Task 5: Document Long-lived Server Policy

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update Hermes Terminal Session Contract**

Add a “Background process governance” subsection:

- `terminal(background=True)` is allowed for dev servers, watchers, and long-running jobs.
- Each LangGraph session-derived `task_id` has a default quota of 3 running background processes.
- Configure with `HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK`.
- Commands that look like long-lived servers/watchers must use `background=True`; Hermes rejects common foreground server patterns.
- Normal turns preserve background processes.
- Explicit session close should call `end_terminal_session(thread_id)`.
- New user messages should call `interrupt_terminal_wait_for_thread_id(thread_id)` before starting/replacing the active run.
- Restart recovery remains best-effort for host-backed processes.

- [ ] **Step 2: Document integration responsibilities**

Document that this repository does not own the external gateway/session-close event. The embedding application is responsible for:

- passing stable `configurable.thread_id`;
- wrapping agent execution with `terminal_execution_scope(thread_id)` if it wants new-message interrupt semantics;
- calling `interrupt_terminal_wait_for_thread_id(thread_id)` when a new message arrives during an active wait;
- calling `end_terminal_session(thread_id)` when the user session is actually closed.

---

## Task 6: Full Verification

**Files:**
- All modified files

- [ ] **Step 1: Run focused tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_terminal_process_policy.py -v
```

- [ ] **Step 2: Run existing shell/session regression tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_execute_command_runtime_smoke.py tests/test_shell_task_id.py tests/test_session_context.py tests/test_hermes_shell_adapter_task_id.py -v
```

- [ ] **Step 3: Compile touched modules**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_tools/terminal_tools.py agent_core/terminal_lifecycle.py agent_core/terminal_process_policy.py
```

- [ ] **Step 4: Check whitespace**

```bash
git diff --check
```

Expected: all checks pass.

---

## Acceptance Criteria

- Starting a fourth running background process for the same `task_id` fails by default with `background_quota_exceeded`.
- Quota is configurable by `HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK`.
- Quota enforcement does not affect foreground commands.
- Quota counts only running sessions for the current runtime-derived `task_id`.
- Existing process ownership checks remain intact.
- `end_terminal_session(thread_id)` kills running processes for that session and cleans the Hermes environment.
- Automatic cleanup refuses missing `thread_id` rather than accidentally targeting Hermes `default`.
- `terminal_execution_scope(thread_id)` registers the active Python execution thread.
- `interrupt_terminal_wait_for_thread_id(thread_id)` signals Hermes interrupt for the registered execution thread.
- Unknown or inactive `thread_id` interrupt calls are no-ops with a structured response.
- `process(action="wait")` remains bounded by `TERMINAL_TIMEOUT` and can return `status="interrupted"` when the outer caller signals a new user message.
- Long-lived servers remain allowed through `terminal(background=True)`, subject to quota and explicit session cleanup.
- README documents the operational contract and embedding responsibilities.

## Risks And Constraints

- The repository does not currently contain the external gateway/session manager. New-message interrupt and automatic session-end cleanup can only be fully activated where user-message and session-close events are received.
- Hermes interrupt is Python-thread-ident based, while LangGraph session identity is `thread_id`; the bridge must register active runs correctly or interrupts will be no-ops.
- If multiple active runs for the same LangGraph `thread_id` are allowed concurrently, the simple mapping should be upgraded from `thread_id -> thread_ident` to `thread_id -> set[thread_ident]`. The recommended product policy is one active run per `thread_id`.
- Long-lived servers can still consume ports, CPU, memory, and disk. Quota reduces process count but does not enforce resource usage; container-level limits or OS-level cgroups are separate concerns.
- Restart recovery is best effort. Host-backed sessions may recover as detached; sandbox-backed process PIDs may be skipped by Hermes.

## Implementation Order

1. Add quota policy helper and tests.
2. Enforce quota in `terminal(background=True)`.
3. Add session-end cleanup API with missing-thread guard.
4. Add active execution registry and new-message interrupt API.
5. Document governance and embedding responsibilities.
6. Run focused and regression verification.

