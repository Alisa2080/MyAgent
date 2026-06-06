# Per-Turn Terminal Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project-style per-turn terminal environment cleanup so non-persistent terminal environments are cleaned after each completed agent turn, while persistent environments remain available for the idle reaper.

**Architecture:** Keep cleanup policy in `agent_core/terminal_lifecycle.py`, because that module already owns thread-id-to-task-id mapping and terminal session lifecycle. Call the policy from `agent_core/agent_runner.py` in `finally` blocks around the initial turn and each terminal-notification resume turn, so cleanup runs for success and error paths. Do not add browser cleanup now: this repository has no browser tool or `cleanup_browser` equivalent, unlike `/home/miku/projects/reference-agent-main/`.

**Tech Stack:** Python, LangGraph/LangChain agent runner, terminal toolkit, pytest.

---

## Reference Behavior

Reference implementation: `/home/miku/projects/reference-agent-main/run_agent.py`

Important behavior to mirror:

- `AIAgent._cleanup_task_resources(task_id)` checks `is_persistent_env(task_id)`.
- If the active terminal environment is persistent, it skips `cleanup_vm(task_id)` and lets the terminal idle reaper clean it later.
- If the active terminal environment is not persistent, it calls `cleanup_vm(task_id)` at turn end.
- Reference code also calls `cleanup_browser(task_id)`, but this repository has no browser resource subsystem, so this plan does not introduce one.

Current repository facts:

- `agent_core/terminal_lifecycle.py` already exposes `end_terminal_session(thread_id)`, but that is explicit session shutdown and kills scoped background processes.
- `agent_tools/terminal_toolkit/terminal_tool.py` already has `cleanup_vm(task_id)`, `is_persistent_env(task_id)`, and the idle reaper.
- `agent_core/agent_runner.py` is the only production-style helper that wraps `agent.invoke(...)`; direct `agent.invoke(...)` callers remain responsible for lifecycle hooks.

## File Structure

- Modify: `agent_core/terminal_lifecycle.py`
  - Add `cleanup_task_resources_for_task_id(task_id, reason="turn_finished")`.
  - Add `cleanup_task_resources_for_thread_id(thread_id, reason="turn_finished")`.
  - Import `is_persistent_env`.
  - Keep `end_terminal_session()` unchanged as explicit session shutdown.

- Modify: `agent_core/agent_runner.py`
  - Add `TERMINAL_PER_TURN_CLEANUP` parsing.
  - Wrap each `agent.invoke(...)` call in a helper that runs `terminal_execution_scope(thread_id)` and then per-turn cleanup in `finally`.
  - Preserve auto-resume behavior and queued-notification behavior.

- Modify: `tests/test_terminal_lifecycle.py`
  - Add tests for non-persistent cleanup, persistent skip, missing thread id, and cleanup error containment.

- Modify: `tests/test_agent_runner.py`
  - Add tests that cleanup happens after the initial turn, after each resume turn, when `agent.invoke` raises, and can be disabled by env var.

- Modify: `README.md`
  - Update lifecycle policy: normal turns clean non-persistent environments; persistent environments survive until idle reaper; explicit shutdown still kills scoped background processes.

---

### Task 1: Add Lifecycle Policy Tests

**Files:**
- Modify: `tests/test_terminal_lifecycle.py`

- [ ] **Step 1: Write failing tests for per-turn cleanup policy**

Add these tests after `test_end_terminal_session_requires_thread_id`:

```python
def test_cleanup_task_resources_for_task_id_cleans_non_persistent_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: False)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: calls.append(task_id))

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result == {
        "task_id": "task-1",
        "cleaned": True,
        "persistent": False,
        "cleanup_reason": "turn_finished",
    }
    assert calls == ["task-1"]


def test_cleanup_task_resources_for_task_id_skips_persistent_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: True)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: calls.append(task_id))

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result == {
        "task_id": "task-1",
        "cleaned": False,
        "persistent": True,
        "cleanup_reason": "turn_finished",
    }
    assert calls == []


def test_cleanup_task_resources_for_task_id_reports_cleanup_error(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: False)

    def fail_cleanup(task_id):
        raise RuntimeError(f"cannot clean {task_id}")

    monkeypatch.setattr(lifecycle, "cleanup_vm", fail_cleanup)

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result["task_id"] == "task-1"
    assert result["cleaned"] is False
    assert result["persistent"] is False
    assert result["cleanup_reason"] == "turn_finished"
    assert "cannot clean task-1" in result["error"]


def test_cleanup_task_resources_for_thread_id_uses_hashed_task_id(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = runtime_task_id_from_thread_id("thread-cleanup")
    calls = []

    monkeypatch.setattr(
        lifecycle,
        "cleanup_task_resources_for_task_id",
        lambda task_id, reason="turn_finished": calls.append((task_id, reason))
        or {"task_id": task_id, "cleaned": True, "persistent": False, "cleanup_reason": reason},
    )

    result = lifecycle.cleanup_task_resources_for_thread_id("thread-cleanup", reason="resume_finished")

    assert result == {
        "task_id": task_id,
        "cleaned": True,
        "persistent": False,
        "cleanup_reason": "resume_finished",
    }
    assert calls == [(task_id, "resume_finished")]


def test_cleanup_task_resources_for_thread_id_requires_thread_id():
    import agent_core.terminal_lifecycle as lifecycle

    result = lifecycle.cleanup_task_resources_for_thread_id(None)

    assert result["cleaned"] is False
    assert result["cleanup_reason"] == "turn_finished"
    assert "thread_id is required" in result["error"]
```

- [ ] **Step 2: Run lifecycle tests and verify they fail**

Run:

```bash
pytest tests/test_terminal_lifecycle.py -q
```

Expected: FAIL with `AttributeError: module 'agent_core.terminal_lifecycle' has no attribute 'cleanup_task_resources_for_task_id'`.

---

### Task 2: Implement Terminal Cleanup Policy

**Files:**
- Modify: `agent_core/terminal_lifecycle.py`

- [ ] **Step 1: Import `is_persistent_env`**

Change the terminal-tool import near the top:

```python
from agent_tools.terminal_toolkit.terminal_tool import cleanup_vm, is_persistent_env
```

- [ ] **Step 2: Add per-turn cleanup helpers**

Insert these functions after `cleanup_terminal_session_for_runtime(...)` and before `end_terminal_session(...)`:

```python
def cleanup_task_resources_for_task_id(
    task_id: str, *, reason: str = "turn_finished"
) -> dict:
    """Clean per-turn terminal resources for a runtime task id.

    Persistent environments intentionally survive normal turn boundaries and are
    left for the reference implementation idle reaper. Non-persistent environments are torn down at
    turn end to avoid leaking sandbox resources.
    """
    try:
        persistent = is_persistent_env(task_id)
    except Exception as exc:
        logger.warning("Failed to inspect terminal environment persistence for %s: %s", task_id, exc)
        persistent = False

    if persistent:
        return {
            "task_id": task_id,
            "cleaned": False,
            "persistent": True,
            "cleanup_reason": reason,
        }

    try:
        cleanup_vm(task_id)
    except Exception as exc:
        logger.warning("Failed to clean terminal environment for %s: %s", task_id, exc)
        return {
            "task_id": task_id,
            "cleaned": False,
            "persistent": False,
            "cleanup_reason": reason,
            "error": str(exc),
        }

    return {
        "task_id": task_id,
        "cleaned": True,
        "persistent": False,
        "cleanup_reason": reason,
    }


def cleanup_task_resources_for_thread_id(
    thread_id: str | None, *, reason: str = "turn_finished"
) -> dict:
    """Clean per-turn terminal resources for a LangGraph thread id."""
    if not thread_id:
        return {
            "cleaned": False,
            "cleanup_reason": reason,
            "error": "thread_id is required for per-turn terminal cleanup",
        }
    task_id = runtime_task_id_from_thread_id(thread_id)
    return cleanup_task_resources_for_task_id(task_id, reason=reason)
```

- [ ] **Step 3: Run lifecycle tests and verify they pass**

Run:

```bash
pytest tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add agent_core/terminal_lifecycle.py tests/test_terminal_lifecycle.py
git commit -m "feat: add per-turn terminal cleanup policy"
```

---

### Task 3: Wire Cleanup Into Agent Runner

**Files:**
- Modify: `tests/test_agent_runner.py`
- Modify: `agent_core/agent_runner.py`

- [ ] **Step 1: Write failing runner cleanup tests**

Add these tests after `test_runner_wraps_initial_and_resume_turns_in_terminal_execution_scope`:

```python
def test_runner_cleans_after_initial_turn(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": ["initial"]}

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert cleanup_calls == [("thread-1", "turn_finished")]


def test_runner_cleans_after_each_resume_turn(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []
    drain_calls = 0

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, input_data, config=None):
            self.calls += 1
            return {"messages": [f"turn-{self.calls}"]}

    def fake_drain(thread_id):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            return [{"type": "completion", "session_id": "proc_1", "command": "job"}]
        return []

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert cleanup_calls == [
        ("thread-1", "turn_finished"),
        ("thread-1", "terminal_notification_resume_finished"),
    ]


def test_runner_cleans_when_agent_invoke_raises(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            raise RuntimeError("model failed")

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    with pytest.raises(RuntimeError, match="model failed"):
        runner.invoke_agent_with_terminal_notifications(
            FakeAgent(),
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
        )

    assert cleanup_calls == [("thread-1", "turn_finished")]


def test_runner_per_turn_cleanup_can_be_disabled(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": ["initial"]}

    monkeypatch.setenv(runner.PER_TURN_CLEANUP_ENV, "false")
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert cleanup_calls == []
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```bash
pytest tests/test_agent_runner.py -q
```

Expected: FAIL with `AttributeError: module 'agent_core.agent_runner' has no attribute 'PER_TURN_CLEANUP_ENV'`.

- [ ] **Step 3: Implement runner cleanup hook**

Modify imports at the top of `agent_core/agent_runner.py`:

```python
from agent_core.terminal_lifecycle import (
    cleanup_task_resources_for_thread_id,
    terminal_execution_scope,
)
```

Add constants after `MAX_AUTO_RESUMES_ENV`:

```python
PER_TURN_CLEANUP_ENV = "TERMINAL_PER_TURN_CLEANUP"
```

Add this helper after `max_terminal_auto_resumes()`:

```python
def per_turn_cleanup_enabled() -> bool:
    raw = os.environ.get(PER_TURN_CLEANUP_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}
```

Add this helper after `per_turn_cleanup_enabled()`:

```python
def _invoke_agent_turn(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None,
    thread_id: str | None,
    *,
    cleanup_reason: str,
) -> Any:
    try:
        with terminal_execution_scope(thread_id):
            return agent.invoke(input_data, config)
    finally:
        if thread_id and per_turn_cleanup_enabled():
            cleanup_task_resources_for_thread_id(thread_id, reason=cleanup_reason)
```

Replace the first invoke block in `invoke_agent_with_terminal_notifications(...)`:

```python
    result = _invoke_agent_turn(
        agent,
        input_data,
        config,
        thread_id,
        cleanup_reason="turn_finished",
    )
```

Replace the resume invoke block:

```python
        result = _invoke_agent_turn(
            agent,
            resume_input,
            config,
            thread_id,
            cleanup_reason="terminal_notification_resume_finished",
        )
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```bash
pytest tests/test_agent_runner.py -q
```

Expected: PASS.

- [ ] **Step 5: Run focused lifecycle and runner tests together**

Run:

```bash
pytest tests/test_terminal_lifecycle.py tests/test_agent_runner.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_core/agent_runner.py tests/test_agent_runner.py
git commit -m "feat: clean non-persistent terminal envs after runner turns"
```

---

### Task 4: Update Runtime Contract Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update lifecycle policy text**

Replace the current `Lifecycle policy:` bullets with:

```markdown
Lifecycle policy:

- Agent turns invoked through `agent_core.agent_runner.invoke_agent_with_terminal_notifications(...)` run per-turn terminal cleanup by default.
- Per-turn cleanup mirrors reference implementation agent behavior: non-persistent terminal environments are cleaned at turn end; persistent environments are left for the reference implementation idle reaper controlled by `TERMINAL_LIFETIME_SECONDS`, defaulting to 300 seconds.
- Set `TERMINAL_PER_TURN_CLEANUP=false` to disable per-turn cleanup for embedding applications that need the previous behavior.
- Direct `agent.invoke(...)` callers are responsible for invoking lifecycle helpers themselves.
- Explicit user-session shutdown should call `end_terminal_session(thread_id)`. Lower-level cleanup helpers remain available as `cleanup_terminal_session_for_thread_id(thread_id)` and `cleanup_terminal_session_for_runtime(runtime)`.
- Explicit shutdown kills running processes for the session task id and cleans the active terminal environment.
- Parent agent startup calls `recover_terminal_processes()`. reference implementation can recover host-backed background processes as detached sessions after restart; sandbox-backed processes are skipped by reference implementation because their in-sandbox PIDs are not meaningful after restart.
```

In `Embedding applications are responsible for terminal lifecycle events:`, add this bullet after the stable `thread_id` bullet:

```markdown
- Prefer `invoke_agent_with_terminal_notifications(...)` over direct `agent.invoke(...)` when you want built-in terminal execution scoping, notification resumes, and per-turn cleanup.
```

- [ ] **Step 2: Run README grep check**

Run:

```bash
rg -n "TERMINAL_PER_TURN_CLEANUP|per-turn terminal cleanup|Normal agent turns preserve background processes" README.md
```

Expected:

- Finds `TERMINAL_PER_TURN_CLEANUP`.
- Finds `per-turn terminal cleanup`.
- Does not find `Normal agent turns preserve background processes`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document per-turn terminal cleanup"
```

---

### Task 5: Final Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_terminal_lifecycle.py tests/test_agent_runner.py tests/test_terminal_tools.py -q
```

Expected: PASS.

- [ ] **Step 2: Run import smoke test**

Run:

```bash
python -c "from agent import agent; from agent_core.agent_runner import invoke_agent_with_terminal_notifications; print('ok')"
```

Expected:

```text
ok
```

- [ ] **Step 3: Inspect diff**

Run:

```bash
git diff --stat
git diff -- agent_core/terminal_lifecycle.py agent_core/agent_runner.py tests/test_terminal_lifecycle.py tests/test_agent_runner.py README.md
```

Expected:

- `terminal_lifecycle.py` contains policy-only cleanup helpers.
- `agent_runner.py` invokes cleanup from `finally`.
- Tests cover persistent skip, non-persistent cleanup, exceptions, resumes, and env opt-out.
- README states the new lifecycle contract.

---

## Risk Notes

- `end_terminal_session(thread_id)` must remain stronger than per-turn cleanup. It intentionally kills scoped background processes and should still be used for explicit user-session shutdown.
- Per-turn cleanup should not call `process_registry.kill_all(...)`; the reference behavior only cleans the active VM/browser resources. Killing all processes would break persistent background process workflows.
- Default local terminal backend has no persistent sandbox filesystem. Its `cleanup_vm(...)` removes local terminal snapshot temp files, so CWD/env snapshot persistence can reset between turns when runner cleanup is enabled.
- Direct `agent.invoke(...)` remains unchanged. That is acceptable because this repository exposes `agent.py` as a LangGraph entrypoint and already documents embedding applications as lifecycle owners.

## Self-Review

- Spec coverage: The plan implements project-style per-turn cleanup, persistent-env skip, idle-reaper handoff, and runner integration. Browser cleanup is explicitly omitted because this repository has no browser subsystem.
- Placeholder scan: No deferred-work placeholders remain.
- Type consistency: `thread_id` remains the LangGraph-facing identifier; `task_id` remains the reference implementation-facing identifier; helper names are consistent across tests and implementation.
