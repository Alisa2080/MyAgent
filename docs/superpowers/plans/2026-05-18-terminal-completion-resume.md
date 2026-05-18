# Terminal Completion Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume Hermes `process_registry.completion_queue` inside the LangChain/LangGraph project runtime and trigger bounded same-thread agent continuation when background terminal processes complete or match watch patterns.

**Architecture:** Add a project-native notification bridge instead of modifying Hermes internals. A small queue-drain module non-blockingly drains Hermes' global `completion_queue`, routes events by runtime-derived Hermes `task_id`, formats scoped events into `[IMPORTANT: ...]` user messages, and an opt-in agent runner invokes the same LangGraph thread again after each turn when relevant background events exist. CLI UI behavior is intentionally out of scope; future CLI/server code should call the runner/helper APIs introduced here.

**Tech Stack:** Python 3.12, LangChain/LangGraph agents, Hermes terminal toolkit `process_registry`, existing `agent_core.session_context` task-id helpers, pytest via `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## Current State

- `agent_tools/terminal_tools.py` already exposes `notify_on_complete` and `watch_patterns` on `terminal(background=True)`.
- Hermes already produces events into `process_registry.completion_queue`.
- Current project has no consumer for that queue, so completion/watch events remain invisible to the LangChain agent.
- Current project has no CLI or server loop in this repository; `agent.py` only exposes `agent = build_agent()`.
- Existing lifecycle APIs already handle startup recovery, session cleanup, and new-message interrupt wiring.

## Scope

In scope:

- Non-blocking drain of Hermes `completion_queue`.
- Per-session routing using LangGraph `thread_id -> Hermes task_id`.
- Event formatting into concise `[IMPORTANT: ...]` user messages.
- Bounded automatic same-thread continuation after an agent turn.
- Tests for routing, formatting, queue safety, and bounded continuation.
- README documentation for embedding applications.

Out of scope:

- CLI UI or REPL loop implementation.
- Push notifications to external clients.
- Websocket/SSE streaming to users.
- Changing Hermes producer behavior unless a minimal `task_id` field is needed for safer routing.

## File Structure

- Create `agent_core/terminal_notifications.py`
  - Owns draining `process_registry.completion_queue`.
  - Routes events into an in-memory per-`task_id` buffer.
  - Formats notification batches for model-visible continuation messages.
  - Does not invoke the agent.

- Create `agent_core/agent_runner.py`
  - Owns opt-in agent invocation helpers.
  - Wraps runs in `terminal_execution_scope(thread_id)`.
  - Drains terminal notifications after each turn.
  - Triggers bounded same-thread continuation with formatted messages.

- Modify `agent_tools/hermes_terminal_toolkit/process_registry.py`
  - Add `task_id` to completion/watch events where the source session is known.
  - Keep existing event fields intact for Hermes compatibility.

- Modify `README.md`
  - Document notification bridge responsibilities and exact API calls for future CLI/server layers.

- Create `tests/test_terminal_notifications.py`
  - Unit tests for drain/routing/formatting.

- Create `tests/test_agent_runner.py`
  - Unit tests for bounded auto-resume behavior without real model calls.

---

### Task 1: Add `task_id` to Hermes Queue Events

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/process_registry.py`
- Test: `tests/test_terminal_notifications.py`

- [ ] **Step 1: Write failing tests for event task routing**

Create `tests/test_terminal_notifications.py` with these initial tests:

```python
from queue import Empty, Queue
from types import SimpleNamespace


def test_drain_routes_completion_event_by_task_id(monkeypatch):
    import agent_core.terminal_notifications as notifications
    from agent_core.session_context import hermes_task_id_from_thread_id

    queue = Queue()
    task_id = hermes_task_id_from_thread_id("thread-1")
    queue.put(
        {
            "type": "completion",
            "task_id": task_id,
            "session_id": "proc_1",
            "command": "python job.py",
            "exit_code": 0,
            "output": "done",
        }
    )

    monkeypatch.setattr(notifications.process_registry, "completion_queue", queue)
    monkeypatch.setattr(notifications, "_pending_events_by_task", {})

    events = notifications.drain_terminal_notifications_for_thread_id("thread-1")

    assert [event["session_id"] for event in events] == ["proc_1"]
    assert queue.empty()


def test_drain_routes_completion_event_by_registry_session_when_task_id_missing(monkeypatch):
    import agent_core.terminal_notifications as notifications
    from agent_core.session_context import hermes_task_id_from_thread_id

    queue = Queue()
    task_id = hermes_task_id_from_thread_id("thread-1")
    queue.put(
        {
            "type": "completion",
            "session_id": "proc_1",
            "command": "python job.py",
            "exit_code": 0,
            "output": "done",
        }
    )

    monkeypatch.setattr(notifications.process_registry, "completion_queue", queue)
    monkeypatch.setattr(notifications, "_pending_events_by_task", {})
    monkeypatch.setattr(
        notifications.process_registry,
        "get",
        lambda session_id: SimpleNamespace(task_id=task_id) if session_id == "proc_1" else None,
    )

    events = notifications.drain_terminal_notifications_for_thread_id("thread-1")

    assert [event["session_id"] for event in events] == ["proc_1"]
    assert queue.empty()


def test_drain_preserves_other_session_events(monkeypatch):
    import agent_core.terminal_notifications as notifications
    from agent_core.session_context import hermes_task_id_from_thread_id

    queue = Queue()
    task_1 = hermes_task_id_from_thread_id("thread-1")
    task_2 = hermes_task_id_from_thread_id("thread-2")
    queue.put({"type": "completion", "task_id": task_2, "session_id": "proc_2", "command": "job2"})
    queue.put({"type": "completion", "task_id": task_1, "session_id": "proc_1", "command": "job1"})

    monkeypatch.setattr(notifications.process_registry, "completion_queue", queue)
    monkeypatch.setattr(notifications, "_pending_events_by_task", {})

    first_events = notifications.drain_terminal_notifications_for_thread_id("thread-1")
    second_events = notifications.drain_terminal_notifications_for_thread_id("thread-2")

    assert [event["session_id"] for event in first_events] == ["proc_1"]
    assert [event["session_id"] for event in second_events] == ["proc_2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_notifications.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.terminal_notifications'`.

- [ ] **Step 3: Add `task_id` to known Hermes producer events**

In `agent_tools/hermes_terminal_toolkit/process_registry.py`, update every event emitted from a known `ProcessSession` to include `"task_id": session.task_id`.

For `_check_watch_patterns()` `watch_disabled` event, include:

```python
"task_id": session.task_id,
```

For `_check_watch_patterns()` `watch_match` event, include:

```python
"task_id": session.task_id,
```

For `_move_to_finished()` completion event, include:

```python
"task_id": session.task_id,
```

For `recover_from_checkpoint()` completion or recovery-related event emitted for a known session, include:

```python
"task_id": session.task_id,
```

Do not add fake task ids to global events such as `watch_overflow_tripped` or `watch_overflow_released`; those are intentionally global and should be routed separately by the notification bridge.

- [ ] **Step 4: Create notification bridge module**

Create `agent_core/terminal_notifications.py`:

```python
from __future__ import annotations

from collections import defaultdict, deque
from queue import Empty
from typing import Any

from agent_core.session_context import hermes_task_id_from_thread_id
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry

_GLOBAL_TASK_ID = "__global__"
_pending_events_by_task: dict[str, deque[dict[str, Any]]] = defaultdict(deque)


def _event_task_id(event: dict[str, Any]) -> str | None:
    task_id = event.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id

    thread_id = event.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return hermes_task_id_from_thread_id(thread_id)

    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        session = process_registry.get(session_id)
        if session is not None and getattr(session, "task_id", None):
            return str(session.task_id)

    event_type = event.get("type")
    if event_type in {"watch_overflow_tripped", "watch_overflow_released"}:
        return _GLOBAL_TASK_ID

    return None


def _drain_completion_queue(*, max_drain: int = 100) -> None:
    drained = 0
    while drained < max_drain:
        try:
            event = process_registry.completion_queue.get_nowait()
        except Empty:
            return
        drained += 1
        if not isinstance(event, dict):
            continue
        task_id = _event_task_id(event)
        if task_id is None:
            _pending_events_by_task[_GLOBAL_TASK_ID].append(event)
            continue
        _pending_events_by_task[task_id].append(event)


def drain_terminal_notifications_for_thread_id(
    thread_id: str | None,
    *,
    max_drain: int = 100,
    max_events: int = 10,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """Return queued Hermes terminal events for this LangGraph thread.

    This drains the global Hermes queue into per-task buffers without dropping
    events for other sessions.
    """
    if not thread_id:
        return []

    _drain_completion_queue(max_drain=max_drain)
    task_id = hermes_task_id_from_thread_id(thread_id)
    events: list[dict[str, Any]] = []

    while _pending_events_by_task[task_id] and len(events) < max_events:
        events.append(_pending_events_by_task[task_id].popleft())

    if include_global:
        while _pending_events_by_task[_GLOBAL_TASK_ID] and len(events) < max_events:
            events.append(_pending_events_by_task[_GLOBAL_TASK_ID].popleft())

    return events
```

- [ ] **Step 5: Run notification routing tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_notifications.py -v
```

Expected: PASS for the three routing tests.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/hermes_terminal_toolkit/process_registry.py agent_core/terminal_notifications.py tests/test_terminal_notifications.py
git commit -m "feat: route Hermes terminal notifications by task"
```

---

### Task 2: Format Hermes Events as Agent Continuation Messages

**Files:**
- Modify: `agent_core/terminal_notifications.py`
- Test: `tests/test_terminal_notifications.py`

- [ ] **Step 1: Add failing formatter tests**

Append these tests to `tests/test_terminal_notifications.py`:

```python
def test_format_completion_notification_message():
    from agent_core.terminal_notifications import format_terminal_notification_message

    message = format_terminal_notification_message(
        [
            {
                "type": "completion",
                "session_id": "proc_1",
                "command": "python job.py",
                "exit_code": 0,
                "output": "finished\n",
            }
        ]
    )

    assert message.startswith("[IMPORTANT: Background terminal update]")
    assert "completion" in message
    assert "proc_1" in message
    assert "exit_code=0" in message
    assert "python job.py" in message
    assert "finished" in message
    assert "Decide whether to inspect logs" in message


def test_format_watch_match_notification_message_truncates_output():
    from agent_core.terminal_notifications import format_terminal_notification_message

    long_output = "x" * 3000
    message = format_terminal_notification_message(
        [
            {
                "type": "watch_match",
                "session_id": "proc_2",
                "command": "npm run dev",
                "pattern": "ERROR",
                "output": long_output,
                "suppressed": 2,
            }
        ],
        max_output_chars=120,
    )

    assert "watch_match" in message
    assert "pattern=ERROR" in message
    assert "suppressed=2" in message
    assert "...(truncated)" in message
    assert len(message) < 900


def test_format_empty_notification_message_is_empty():
    from agent_core.terminal_notifications import format_terminal_notification_message

    assert format_terminal_notification_message([]) == ""
```

- [ ] **Step 2: Run tests to verify formatter fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_notifications.py -v
```

Expected: FAIL with `ImportError` or `AttributeError` for `format_terminal_notification_message`.

- [ ] **Step 3: Implement formatter helpers**

Append this implementation to `agent_core/terminal_notifications.py`:

```python
def _shorten(value: Any, *, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(truncated)"


def _format_event(event: dict[str, Any], *, max_output_chars: int) -> str:
    event_type = str(event.get("type") or "unknown")
    session_id = str(event.get("session_id") or "")
    command = _shorten(event.get("command") or "", max_chars=240)

    parts = [f"- type={event_type}"]
    if session_id:
        parts.append(f"session_id={session_id}")
    if event_type == "completion":
        parts.append(f"exit_code={event.get('exit_code')}")
    if event_type == "watch_match":
        parts.append(f"pattern={event.get('pattern')}")
        parts.append(f"suppressed={event.get('suppressed', 0)}")
    if event_type in {"watch_disabled", "watch_overflow_tripped", "watch_overflow_released"}:
        message = event.get("message")
        if message:
            parts.append(f"message={_shorten(message, max_chars=300)}")

    header = " ".join(parts)
    output = _shorten(event.get("output") or "", max_chars=max_output_chars)
    body = f"{header}\n  command: {command}"
    if output:
        body += f"\n  output:\n{output}"
    return body


def format_terminal_notification_message(
    events: list[dict[str, Any]],
    *,
    max_output_chars: int = 1200,
) -> str:
    """Format Hermes background events as one model-visible continuation message."""
    if not events:
        return ""

    formatted_events = "\n\n".join(
        _format_event(event, max_output_chars=max_output_chars)
        for event in events
    )
    return (
        "[IMPORTANT: Background terminal update]\n"
        "One or more Hermes background processes produced notifications for this conversation.\n"
        f"{formatted_events}\n\n"
        "Decide whether to inspect logs with process(action='log'|'poll'), continue the task, "
        "report completion to the user, or kill the process if it is no longer needed."
    )
```

- [ ] **Step 4: Run formatter tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/terminal_notifications.py tests/test_terminal_notifications.py
git commit -m "feat: format terminal completion notifications"
```

---

### Task 3: Add Bounded Agent Auto-Resume Runner

**Files:**
- Create: `agent_core/agent_runner.py`
- Test: `tests/test_agent_runner.py`

- [ ] **Step 1: Write failing runner tests**

Create `tests/test_agent_runner.py`:

```python
def test_invoke_agent_with_terminal_notifications_runs_initial_turn_only_without_events(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert len(calls) == 1


def test_invoke_agent_with_terminal_notifications_resumes_same_thread(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []
    drain_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": [f"turn-{len(calls)}"]}

    def fake_drain(thread_id):
        drain_calls.append(thread_id)
        if len(drain_calls) == 1:
            return [{"type": "completion", "session_id": "proc_1", "command": "python job.py", "exit_code": 0}]
        return []

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert len(calls) == 2
    assert calls[1][0] == {"messages": [{"role": "user", "content": "formatted notification"}]}
    assert calls[1][1] == {"configurable": {"thread_id": "thread-1"}}


def test_invoke_agent_with_terminal_notifications_respects_max_auto_resumes(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append(input_data)
            return {"messages": [f"turn-{len(calls)}"]}

    monkeypatch.setattr(
        runner,
        "drain_terminal_notifications_for_thread_id",
        lambda thread_id: [{"type": "completion", "session_id": "proc_1", "command": "job"}],
    )
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        max_auto_resumes=2,
    )

    assert result == {"messages": ["turn-3"]}
    assert len(calls) == 3
```

- [ ] **Step 2: Run tests to verify runner module fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.agent_runner'`.

- [ ] **Step 3: Implement runner module**

Create `agent_core/agent_runner.py`:

```python
from __future__ import annotations

import os
from typing import Any

from agent_core.terminal_lifecycle import terminal_execution_scope
from agent_core.terminal_notifications import (
    drain_terminal_notifications_for_thread_id,
    format_terminal_notification_message,
)

DEFAULT_MAX_AUTO_RESUMES = 3
MAX_AUTO_RESUMES_ENV = "HERMES_TERMINAL_MAX_AUTO_RESUMES"


def _thread_id_from_config(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("thread_id")
    return str(value) if value else None


def max_terminal_auto_resumes() -> int:
    raw = os.environ.get(MAX_AUTO_RESUMES_ENV)
    if raw is None:
        return DEFAULT_MAX_AUTO_RESUMES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AUTO_RESUMES
    return max(0, value)


def invoke_agent_with_terminal_notifications(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    max_auto_resumes: int | None = None,
) -> Any:
    """Invoke an agent and continue the same thread for queued terminal events.

    This helper is intended for embedding layers such as a future CLI or server.
    It does not change the exported LangGraph graph in `agent.py`.
    """
    thread_id = _thread_id_from_config(config)
    resume_limit = max_terminal_auto_resumes() if max_auto_resumes is None else max(0, max_auto_resumes)

    with terminal_execution_scope(thread_id):
        result = agent.invoke(input_data, config)

    if not thread_id:
        return result

    for _ in range(resume_limit):
        events = drain_terminal_notifications_for_thread_id(thread_id)
        if not events:
            break
        message = format_terminal_notification_message(events)
        if not message:
            break
        with terminal_execution_scope(thread_id):
            result = agent.invoke(
                {"messages": [{"role": "user", "content": message}]},
                config,
            )

    return result
```

- [ ] **Step 4: Run runner tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/agent_runner.py tests/test_agent_runner.py
git commit -m "feat: auto-resume agent on terminal notifications"
```

---

### Task 4: Integrate Queue Drain After Normal Agent Turns Without CLI Coupling

**Files:**
- Modify: `README.md`
- Test: `tests/test_agent_runner.py`

- [ ] **Step 1: Add tests for thread-id requirement and execution scope**

Append these tests to `tests/test_agent_runner.py`:

```python
def test_runner_does_not_resume_without_thread_id(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    monkeypatch.setattr(
        runner,
        "drain_terminal_notifications_for_thread_id",
        lambda thread_id: (_ for _ in ()).throw(AssertionError("should not drain without thread_id")),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {},
    )

    assert result == {"messages": ["initial"]}
    assert len(calls) == 1


def test_runner_wraps_initial_and_resume_turns_in_terminal_execution_scope(monkeypatch):
    import contextlib

    import agent_core.agent_runner as runner

    scopes = []

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, input_data, config=None):
            self.calls += 1
            return {"messages": [f"turn-{self.calls}"]}

    @contextlib.contextmanager
    def fake_scope(thread_id):
        scopes.append(("enter", thread_id))
        try:
            yield
        finally:
            scopes.append(("exit", thread_id))

    drain_count = 0

    def fake_drain(thread_id):
        nonlocal drain_count
        drain_count += 1
        if drain_count == 1:
            return [{"type": "completion", "session_id": "proc_1", "command": "job"}]
        return []

    monkeypatch.setattr(runner, "terminal_execution_scope", fake_scope)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")

    runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert scopes == [
        ("enter", "thread-1"),
        ("exit", "thread-1"),
        ("enter", "thread-1"),
        ("exit", "thread-1"),
    ]
```

- [ ] **Step 2: Run runner tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_runner.py -v
```

Expected: PASS.

- [ ] **Step 3: Update README embedding contract**

In `README.md`, add this subsection under `Embedding applications are responsible for terminal lifecycle events:`:

```markdown
Background completion notifications:

- Hermes queues background process events in `process_registry.completion_queue` when `terminal(background=True, notify_on_complete=True)` exits or when `watch_patterns` match.
- This project consumes those events through `agent_core.terminal_notifications.drain_terminal_notifications_for_thread_id(thread_id)`.
- Embedding applications that want automatic continuation should call `agent_core.agent_runner.invoke_agent_with_terminal_notifications(agent, input_data, config)`.
- The runner uses the same `configurable.thread_id` for continuation messages, so the resumed turn stays in the same LangGraph conversation thread and maps to the same Hermes `task_id`.
- Auto-resume is bounded by `HERMES_TERMINAL_MAX_AUTO_RESUMES`, default `3`, to avoid loops caused by noisy background processes.
- CLI-specific display, idle polling, websocket delivery, and user-facing push notifications are not implemented here; future CLI/server code should build on these helper APIs.
```

- [ ] **Step 4: Commit**

```bash
git add README.md tests/test_agent_runner.py
git commit -m "docs: document terminal notification runner contract"
```

---

### Task 5: Add End-to-End Queue-to-Resume Test

**Files:**
- Modify: `tests/test_agent_runner.py`

- [ ] **Step 1: Add queue-to-resume test**

Append this test to `tests/test_agent_runner.py`:

```python
from queue import Queue


def test_runner_drains_real_queue_and_passes_formatted_notification(monkeypatch):
    import agent_core.agent_runner as runner
    import agent_core.terminal_notifications as notifications
    from agent_core.session_context import hermes_task_id_from_thread_id

    task_id = hermes_task_id_from_thread_id("thread-1")
    queue = Queue()
    queue.put(
        {
            "type": "completion",
            "task_id": task_id,
            "session_id": "proc_1",
            "command": "python job.py",
            "exit_code": 0,
            "output": "done",
        }
    )

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": [f"turn-{len(calls)}"]}

    monkeypatch.setattr(notifications.process_registry, "completion_queue", queue)
    monkeypatch.setattr(notifications, "_pending_events_by_task", {})
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", notifications.drain_terminal_notifications_for_thread_id)
    monkeypatch.setattr(runner, "format_terminal_notification_message", notifications.format_terminal_notification_message)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert len(calls) == 2
    resume_message = calls[1][0]["messages"][0]["content"]
    assert "[IMPORTANT: Background terminal update]" in resume_message
    assert "proc_1" in resume_message
    assert "done" in resume_message
```

- [ ] **Step 2: Run end-to-end runner tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_runner.py tests/test_terminal_notifications.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_agent_runner.py
git commit -m "test: cover terminal notification auto-resume"
```

---

### Task 6: Full Verification and Review

**Files:**
- No implementation files unless verification exposes failures.

- [ ] **Step 1: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_notifications.py tests/test_agent_runner.py tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_terminal_process_policy.py tests/test_system_prompt.py tests/test_tool_limits.py tests/test_session_context.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: PASS.

- [ ] **Step 3: Compile changed modules**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_core/terminal_notifications.py agent_core/agent_runner.py agent_core/terminal_lifecycle.py agent_tools/terminal_tools.py agent_tools/hermes_terminal_toolkit/process_registry.py
```

Expected: no output and exit code `0`.

- [ ] **Step 4: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output and exit code `0`.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` with scope:

```text
Review range: main..HEAD
Focus: Hermes completion_queue consumption, per-task notification routing, event formatting, bounded same-thread auto-resume, no CLI coupling, lifecycle compatibility, tests, and README contract.
```

Expected: reviewer reports no Critical/Important findings, or findings are fixed through `superpowers:receiving-code-review`.

- [ ] **Step 6: Final commit if verification changed files**

If verification or review fixes changed files, commit:

```bash
git add agent_core/terminal_notifications.py agent_core/agent_runner.py agent_tools/hermes_terminal_toolkit/process_registry.py tests/test_terminal_notifications.py tests/test_agent_runner.py README.md
git commit -m "fix: harden terminal notification resume flow"
```

---

## Design Notes

- Queue consumption must be non-blocking. Agent runs should never wait indefinitely for background events.
- Draining the global Hermes queue is destructive, so events for other sessions must be buffered instead of dropped.
- `task_id` is still hidden from model-visible tool schemas. It is only used internally for event routing.
- The continuation message is intentionally a user message, matching Hermes CLI's `_pending_input` approach, but the content is generated by trusted project code.
- Auto-resume is opt-in via `invoke_agent_with_terminal_notifications()` because this repository currently lacks a server/CLI event loop.
- The exported `agent` in `agent.py` should not be wrapped until there is an explicit runtime that owns user-session semantics. LangGraph Studio or other embedders may expect a plain graph.
- Global watch overflow events do not include user output and may be delivered to the next session-specific drain. This is acceptable for now because they describe toolkit-wide throttling, not private process data.

## Self-Review

- Spec coverage: The plan consumes Hermes `completion_queue`, handles `completion`, `watch_match`, `watch_disabled`, and global overflow event types, and triggers bounded same-thread continuation. CLI work is explicitly excluded.
- Placeholder scan: No `TBD`, vague "add tests", or undefined implementation steps remain.
- Type consistency: `thread_id` remains LangGraph-facing, `task_id` remains Hermes-facing, and the new runner accepts the same `agent.invoke(input_data, config)` shape already used by this codebase.

