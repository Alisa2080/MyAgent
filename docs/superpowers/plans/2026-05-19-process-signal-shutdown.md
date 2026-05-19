# Process Signal Shutdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project-level SIGTERM/SIGHUP shutdown handling so LangGraph agent process exits interrupt foreground terminal waits, terminate Hermes background processes, and clean terminal environments without orphaning subprocess groups.

**Architecture:** Keep Hermes terminal process mechanics unchanged: foreground commands already run in their own process group and `_wait_for_process()` already kills that group after `is_interrupted()` becomes true. Add a thin LangChain project lifecycle layer that broadcasts interrupt to every active `terminal_execution_scope()` thread, sleeps for a configurable grace window to let daemon/tool threads observe the interrupt, then raises `KeyboardInterrupt`; an idempotent atexit cleanup kills all registered background processes and cleans all Hermes environments.

**Tech Stack:** Python 3.12, LangChain/LangGraph `create_agent`, existing `agent_core.terminal_lifecycle`, Hermes terminal toolkit `process_registry`, pytest via `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## Current State

- `agent_tools/hermes_terminal_toolkit/environments/local.py` starts foreground shell commands with `preexec_fn=os.setsid` on Unix, so shell children form a separate process group.
- `agent_tools/hermes_terminal_toolkit/environments/base.py` already polls every 200ms and calls `_kill_process(proc)` when `is_interrupted()` is true.
- `agent_tools/hermes_terminal_toolkit/process_registry.py` already supports `kill_all(task_id=None)` for all running background sessions.
- `agent_core.terminal_lifecycle.terminal_execution_scope(thread_id)` records active Python execution thread ids, and `interrupt_terminal_wait_for_thread_id(thread_id)` can interrupt one LangGraph thread.
- `agent_tools/hermes_terminal_toolkit/terminal_tool.py` already registers `cleanup_all_environments` with `atexit`, but it does not kill every running background session before environment cleanup.
- This repository has no standalone CLI loop. `agent.py` exposes `agent = build_agent()` for LangGraph, so signal registration should be process-level, idempotent, and installed from `build_agent()`.

## File Structure

- Modify `agent_core/terminal_lifecycle.py`
  - Add active-execution snapshot and all-thread interrupt helpers.
  - Keep existing per-thread APIs unchanged for new-user-message interrupt behavior.

- Create `agent_core/process_lifecycle.py`
  - Own process signal registration, signal handler, grace-window parsing, and atexit cleanup.
  - Do not import or depend on LangGraph runtime objects.

- Modify `agent_core/builders.py`
  - Install process signal handlers once during `build_agent()` before terminal process recovery.

- Create `tests/test_process_lifecycle.py`
  - Unit tests for grace parsing, signal registration, handler ordering, and idempotent cleanup.

- Modify `tests/test_terminal_lifecycle.py`
  - Add tests for all-active-thread interrupt helpers.

- Modify `README.md`
  - Document signal behavior, `HERMES_SIGTERM_GRACE`, and direct `agent.invoke(...)` responsibilities.

---

### Task 1: Add All-Thread Terminal Interrupt Helpers

**Files:**
- Modify: `agent_core/terminal_lifecycle.py`
- Test: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Write failing tests for process-wide interrupt broadcast**

Append these tests to `tests/test_terminal_lifecycle.py`:

```python
def test_snapshot_active_terminal_execution_threads_returns_copy(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": {11, 22}})

    snapshot = lifecycle.snapshot_active_terminal_execution_threads()
    snapshot["thread-1"].add(33)

    assert snapshot == {"thread-1": {11, 22, 33}}
    assert lifecycle._active_execution_threads == {"thread-1": {11, 22}}


def test_interrupt_all_terminal_waits_signals_every_active_python_thread(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": {11, 22}, "thread-2": {33}})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    result = lifecycle.interrupt_all_terminal_waits(reason="received_signal_15")

    assert result == {
        "interrupted": True,
        "reason": "received_signal_15",
        "python_thread_ids": [11, 22, 33],
        "thread_ids": ["thread-1", "thread-2"],
    }
    assert calls == [(True, 11), (True, 22), (True, 33)]


def test_interrupt_all_terminal_waits_is_noop_without_active_threads(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    result = lifecycle.interrupt_all_terminal_waits(reason="received_signal_1")

    assert result == {
        "interrupted": False,
        "reason": "received_signal_1",
        "python_thread_ids": [],
        "thread_ids": [],
    }
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -k "snapshot_active_terminal_execution_threads or interrupt_all_terminal_waits" -v
```

Expected: FAIL with `AttributeError` for `snapshot_active_terminal_execution_threads` or `interrupt_all_terminal_waits`.

- [ ] **Step 3: Implement active-thread snapshot and broadcast**

In `agent_core/terminal_lifecycle.py`, add these functions after `terminal_execution_scope(...)` and before `interrupt_terminal_wait_for_thread_id(...)`:

```python
def snapshot_active_terminal_execution_threads() -> dict[str, set[int]]:
    """Return a copy of active LangGraph thread ids to Python execution thread ids."""
    with _active_execution_lock:
        return {
            thread_id: set(python_thread_ids)
            for thread_id, python_thread_ids in _active_execution_threads.items()
        }


def interrupt_all_terminal_waits(*, reason: str = "process_signal") -> dict:
    """Signal every active terminal/process wait in this Python process."""
    snapshot = snapshot_active_terminal_execution_threads()
    python_thread_ids = sorted(
        python_thread_id
        for thread_ids in snapshot.values()
        for python_thread_id in thread_ids
    )

    for python_thread_id in python_thread_ids:
        set_interrupt(True, thread_id=python_thread_id)

    return {
        "interrupted": bool(python_thread_ids),
        "reason": reason,
        "python_thread_ids": python_thread_ids,
        "thread_ids": sorted(snapshot),
    }
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_lifecycle.py -k "snapshot_active_terminal_execution_threads or interrupt_all_terminal_waits" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_core/terminal_lifecycle.py tests/test_terminal_lifecycle.py
git commit -m "feat: broadcast terminal interrupts to active runs"
```

---

### Task 2: Add Process Signal Lifecycle Module

**Files:**
- Create: `agent_core/process_lifecycle.py`
- Test: `tests/test_process_lifecycle.py`

- [ ] **Step 1: Write failing tests for grace parsing and signal handler behavior**

Create `tests/test_process_lifecycle.py` with:

```python
import pytest


def test_parse_signal_grace_defaults_to_one_point_five(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.delenv(lifecycle.SIGTERM_GRACE_ENV, raising=False)

    assert lifecycle.signal_grace_seconds() == 1.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        ("2.25", 2.25),
        ("-1", 0.0),
        ("invalid", 1.5),
    ],
)
def test_parse_signal_grace_env(monkeypatch, raw, expected):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setenv(lifecycle.SIGTERM_GRACE_ENV, raw)

    assert lifecycle.signal_grace_seconds() == expected


def test_signal_handler_interrupts_sleeps_then_raises(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason)) or {"interrupted": True},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.25)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert calls == [("interrupt", "received_signal_15"), ("sleep", 0.25)]


def test_signal_handler_skips_sleep_when_grace_is_zero(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason)) or {"interrupted": True},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.0)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(1, None)

    assert calls == [("interrupt", "received_signal_1")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.process_lifecycle'`.

- [ ] **Step 3: Implement lifecycle module with signal handler**

Create `agent_core/process_lifecycle.py`:

```python
from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
import time
from types import FrameType
from typing import Any

from agent_core.terminal_lifecycle import interrupt_all_terminal_waits
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry
from agent_tools.hermes_terminal_toolkit.terminal_tool import cleanup_all_environments

logger = logging.getLogger(__name__)

SIGTERM_GRACE_ENV = "HERMES_SIGTERM_GRACE"
DEFAULT_SIGTERM_GRACE_SECONDS = 1.5

_install_lock = threading.Lock()
_installed = False
_cleanup_lock = threading.Lock()
_cleanup_done = False


def signal_grace_seconds() -> float:
    raw = os.getenv(SIGTERM_GRACE_ENV)
    if raw is None:
        return DEFAULT_SIGTERM_GRACE_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_SIGTERM_GRACE_SECONDS


def _signal_handler(signum: int, frame: FrameType | None) -> None:
    try:
        interrupt_all_terminal_waits(reason=f"received_signal_{signum}")
        grace = signal_grace_seconds()
        if grace > 0:
            time.sleep(grace)
    except Exception:
        logger.exception("Failed while handling process signal %s.", signum)
    raise KeyboardInterrupt()


def run_process_shutdown_cleanup(*, reason: str = "process_exit") -> dict[str, Any]:
    global _cleanup_done
    with _cleanup_lock:
        if _cleanup_done:
            return {"cleaned": False, "reason": reason, "already_done": True}
        _cleanup_done = True

    killed_processes = 0
    cleaned_environments = 0
    errors: list[str] = []

    try:
        killed_processes = process_registry.kill_all()
    except Exception as exc:
        logger.exception("Failed to kill Hermes background processes during shutdown.")
        errors.append(f"process_registry.kill_all: {exc}")

    try:
        cleaned_environments = cleanup_all_environments()
    except Exception as exc:
        logger.exception("Failed to clean Hermes environments during shutdown.")
        errors.append(f"cleanup_all_environments: {exc}")

    return {
        "cleaned": True,
        "reason": reason,
        "killed_processes": killed_processes,
        "cleaned_environments": cleaned_environments,
        "errors": errors,
    }


def _atexit_cleanup() -> None:
    run_process_shutdown_cleanup(reason="atexit")


def install_process_signal_handlers() -> bool:
    global _installed
    with _install_lock:
        if _installed:
            return False

        signal.signal(signal.SIGTERM, _signal_handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _signal_handler)
        atexit.register(_atexit_cleanup)
        _installed = True
        return True
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py -k "grace or signal_handler" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_core/process_lifecycle.py tests/test_process_lifecycle.py
git commit -m "feat: handle process termination signals"
```

---

### Task 3: Add Idempotent Atexit Cleanup Tests

**Files:**
- Modify: `tests/test_process_lifecycle.py`
- Modify: `agent_core/process_lifecycle.py`

- [ ] **Step 1: Add failing tests for cleanup ordering and idempotency**

Append to `tests/test_process_lifecycle.py`:

```python
def test_run_process_shutdown_cleanup_kills_processes_before_environments(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda: calls.append("kill_all") or 2)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: calls.append("cleanup_envs") or 3)

    result = lifecycle.run_process_shutdown_cleanup(reason="test")

    assert result == {
        "cleaned": True,
        "reason": "test",
        "killed_processes": 2,
        "cleaned_environments": 3,
        "errors": [],
    }
    assert calls == ["kill_all", "cleanup_envs"]


def test_run_process_shutdown_cleanup_runs_once(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda: calls.append("kill_all") or 1)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: calls.append("cleanup_envs") or 1)

    first = lifecycle.run_process_shutdown_cleanup(reason="first")
    second = lifecycle.run_process_shutdown_cleanup(reason="second")

    assert first["cleaned"] is True
    assert second == {"cleaned": False, "reason": "second", "already_done": True}
    assert calls == ["kill_all", "cleanup_envs"]


def test_run_process_shutdown_cleanup_reports_errors_and_continues(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_cleanup_done", False)

    def fail_kill_all():
        raise RuntimeError("kill failed")

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", fail_kill_all)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: 4)

    result = lifecycle.run_process_shutdown_cleanup(reason="test")

    assert result["cleaned"] is True
    assert result["killed_processes"] == 0
    assert result["cleaned_environments"] == 4
    assert result["errors"] == ["process_registry.kill_all: kill failed"]
```

- [ ] **Step 2: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py -k "shutdown_cleanup" -v
```

Expected: PASS if Task 2 implementation already matches the required cleanup semantics. If any assertion fails, adjust only `agent_core/process_lifecycle.py` to match the tests.

- [ ] **Step 3: Commit**

Run:

```bash
git add agent_core/process_lifecycle.py tests/test_process_lifecycle.py
git commit -m "test: cover shutdown cleanup ordering"
```

---

### Task 4: Register Signal Handlers from Agent Construction

**Files:**
- Modify: `agent_core/builders.py`
- Modify: `tests/test_process_lifecycle.py`
- Modify: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Add failing tests for registration idempotency**

Append to `tests/test_process_lifecycle.py`:

```python
def test_install_process_signal_handlers_registers_sigterm_sighup_and_atexit(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    signal_calls = []
    atexit_calls = []
    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda signum, handler: signal_calls.append((signum, handler)))
    monkeypatch.setattr(lifecycle.atexit, "register", lambda handler: atexit_calls.append(handler))

    installed = lifecycle.install_process_signal_handlers()

    assert installed is True
    assert (lifecycle.signal.SIGTERM, lifecycle._signal_handler) in signal_calls
    if hasattr(lifecycle.signal, "SIGHUP"):
        assert (lifecycle.signal.SIGHUP, lifecycle._signal_handler) in signal_calls
    assert atexit_calls == [lifecycle._atexit_cleanup]


def test_install_process_signal_handlers_is_idempotent(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    signal_calls = []
    atexit_calls = []
    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda signum, handler: signal_calls.append((signum, handler)))
    monkeypatch.setattr(lifecycle.atexit, "register", lambda handler: atexit_calls.append(handler))

    assert lifecycle.install_process_signal_handlers() is True
    assert lifecycle.install_process_signal_handlers() is False
    assert len(atexit_calls) == 1
    assert len([call for call in signal_calls if call[0] == lifecycle.signal.SIGTERM]) == 1
```

- [ ] **Step 2: Modify existing build-agent lifecycle test**

In `tests/test_terminal_lifecycle.py`, update `test_build_agent_recovers_terminal_processes_after_loading_memory` by adding this monkeypatch before `result = builders.build_agent()`:

```python
monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: calls.append("install_signals"))
```

Then update the call-order assertion:

```python
assert calls == ["install_signals", "load", "recover"]
```

- [ ] **Step 3: Run tests to verify builder import fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py::test_build_agent_recovers_terminal_processes_after_loading_memory -v
```

Expected: `tests/test_process_lifecycle.py` should pass after Task 2/3. `test_build_agent_recovers_terminal_processes_after_loading_memory` should FAIL with `AttributeError` because `builders.install_process_signal_handlers` is not imported yet.

- [ ] **Step 4: Integrate install call in builder**

In `agent_core/builders.py`, add this import:

```python
from agent_core.process_lifecycle import install_process_signal_handlers
```

Then update the start of `build_agent()`:

```python
def build_agent():
    install_process_signal_handlers()
    memory_store.load_from_disk()
    recover_terminal_processes()
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py::test_build_agent_recovers_terminal_processes_after_loading_memory -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add agent_core/builders.py tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py
git commit -m "feat: install shutdown signal handlers at startup"
```

---

### Task 5: Add a Real Foreground Command Shutdown Regression Test

**Files:**
- Modify: `tests/test_process_lifecycle.py`

- [ ] **Step 1: Add a test that proves signal handler gives poll loop time to kill a process group**

Append to `tests/test_process_lifecycle.py`:

```python
import threading
import time


def test_signal_handler_grace_window_allows_foreground_wait_to_kill_process(monkeypatch):
    import agent_core.process_lifecycle as process_lifecycle
    from agent_core.terminal_lifecycle import terminal_execution_scope
    from agent_tools.hermes_terminal_toolkit.environments.local import LocalEnvironment

    env = LocalEnvironment()
    result_holder = {}
    ready = threading.Event()

    def run_command():
        with terminal_execution_scope("signal-thread"):
            ready.set()
            result_holder["result"] = env.execute("sleep 30", timeout=60)

    worker = threading.Thread(target=run_command)
    worker.start()
    assert ready.wait(timeout=5)
    time.sleep(0.3)

    monkeypatch.setattr(process_lifecycle, "signal_grace_seconds", lambda: 0.8)

    with pytest.raises(KeyboardInterrupt):
        process_lifecycle._signal_handler(15, None)

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result_holder["result"]["returncode"] == 130
    assert "[Command interrupted]" in result_holder["result"]["output"]
```

- [ ] **Step 2: Run the regression test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py::test_signal_handler_grace_window_allows_foreground_wait_to_kill_process -v
```

Expected: PASS in about 1 second. Failure modes:

- If the worker is still alive, the broadcast interrupt did not reach the worker thread.
- If the result return code is not `130`, `_wait_for_process()` did not take the interrupt branch.
- If the test takes close to 30 seconds, the process group was not killed.

- [ ] **Step 3: Commit**

Run:

```bash
git add tests/test_process_lifecycle.py
git commit -m "test: verify signal grace interrupts foreground commands"
```

---

### Task 6: Document Process Shutdown Contract

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README signal lifecycle section**

In `README.md`, under `Lifecycle policy:`, add:

```markdown
- Parent agent startup installs process-level SIGTERM and SIGHUP handlers once per Python process.
- On SIGTERM or SIGHUP, the handler marks every active `terminal_execution_scope(thread_id)` thread as interrupted, sleeps for `HERMES_SIGTERM_GRACE` seconds, then raises `KeyboardInterrupt` so the hosting runtime can terminate the run.
- `HERMES_SIGTERM_GRACE` defaults to `1.5`. Set it to `0` to skip the grace window, or increase it if the host routinely runs terminal commands with slow shutdown behavior.
- The grace window is intentionally present so foreground terminal poll loops can observe the interrupt and kill their subprocess groups before Python exits.
- At Python process exit, project shutdown cleanup calls `process_registry.kill_all()` before `cleanup_all_environments()`. This covers background Hermes sessions in addition to active terminal environments.
```

Under `Embedding applications are responsible for terminal lifecycle events:`, add:

```markdown
- Direct `agent.invoke(...)` callers still get process signal cleanup after `build_agent()` installs handlers, but they do not get per-turn cleanup, notification resume, or new-user-message interrupt unless they use the runner/helper APIs above.
```

- [ ] **Step 2: Run documentation-adjacent smoke tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

Run:

```bash
git add README.md
git commit -m "docs: describe process signal shutdown lifecycle"
```

---

### Task 7: Final Verification

**Files:**
- No code changes unless verification exposes a failure.

- [ ] **Step 1: Run full project tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: PASS.

- [ ] **Step 2: Run import smoke check**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -c "from agent import agent; from agent_core.process_lifecycle import install_process_signal_handlers; print('ok')"
```

Expected output:

```text
ok
```

- [ ] **Step 3: Inspect changed files**

Run:

```bash
git diff -- agent_core/terminal_lifecycle.py agent_core/process_lifecycle.py agent_core/builders.py tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py README.md
```

Expected: diff only contains the signal lifecycle helpers, tests, builder install call, and docs described in this plan.

- [ ] **Step 4: Commit final fixes if needed**

If verification required any fixes, commit them:

```bash
git add agent_core/terminal_lifecycle.py agent_core/process_lifecycle.py agent_core/builders.py tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py README.md
git commit -m "fix: stabilize shutdown signal lifecycle"
```

If no fixes were needed, skip this commit.

---

## Self-Review

- Spec coverage: The plan maps Hermes' five-step chain to this repository: signal registration in `build_agent()`, interrupt broadcast through `agent_core.terminal_lifecycle`, grace window in `agent_core.process_lifecycle`, `KeyboardInterrupt` propagation, and atexit cleanup that includes both background process registry and terminal environments.
- Project fit: The plan accounts for this repository's lack of CLI/server loop by installing process-level handlers from the LangGraph entry path rather than adding CLI-only code.
- Orphan prevention: Foreground commands remain handled by Hermes `_wait_for_process()` and `_kill_process()`. Background sessions are covered by `process_registry.kill_all()` during atexit cleanup.
- Placeholder scan: No task contains TBD-style implementation gaps; code snippets and commands are concrete.
- Type consistency: `thread_id` means LangGraph thread id string; `python_thread_id` means `threading.Thread.ident`; `task_id` remains the Hermes hashed task id managed by existing session-context helpers.
