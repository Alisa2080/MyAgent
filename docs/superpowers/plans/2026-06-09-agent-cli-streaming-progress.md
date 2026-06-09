# Agent CLI Streaming Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add default concise progress output to `agent_cli` so long agent turns show streamed assistant text and visible tool lifecycle updates before the final response.

**Architecture:** Add a neutral core progress event module, a CLI terminal observer, ToolBus event emission, and runner streaming fallback. The CLI passes an observer through LangGraph config; core code emits structured events without importing `agent_cli`; `AgentCLI.submit_message()` suppresses duplicate final printing when the response has already streamed.

**Tech Stack:** Python, pytest, LangChain/LangGraph agent runnable APIs, existing `agent_cli`, `agent_core`, and `ToolBusMiddleware`.

---

## File Structure

- Create `agent_core/progress.py`
  - Neutral event dataclasses, observer protocol, safe emit helper, config/runtime observer extraction, and streamed-result marker.
  - Must not import `agent_cli`.
- Create `agent_cli/progress.py`
  - `TerminalProgressObserver`, concise terminal rendering, token buffering/flush, tool preview helpers, and tool-result summary helpers.
  - May import `agent_core.progress` event types.
- Modify `agent_core/agent_runner.py`
  - Add optional observer support and stream-first invocation with invoke fallback.
  - Preserve existing terminal notification auto-resume behavior.
- Modify `agent_core/tool_bus_middleware.py`
  - Emit `tool_start`, `tool_complete`, and `tool_error` events via observer found in runtime config.
- Modify `agent_cli/repl.py`
  - Extend runner signature handling.
  - Add optional `progress_observer_factory`.
  - Pass observer through config.
  - Avoid duplicate final output after streamed text.
- Modify `agent_cli/main.py`
  - Inject the default observer factory for real CLI commands.
- Add/modify tests:
  - `tests/test_agent_progress.py`
  - `tests/test_agent_cli_progress.py`
  - `tests/test_agent_runner.py`
  - `tests/test_tool_bus_middleware.py`
  - `tests/test_agent_cli_repl.py`
  - `tests/test_agent_cli_main.py`

## Task 1: Core Progress Event Contract

**Files:**
- Create: `agent_core/progress.py`
- Test: `tests/test_agent_progress.py`

- [ ] **Step 1: Write failing tests for safe observer emission and config extraction**

Create `tests/test_agent_progress.py`:

```python
from __future__ import annotations

from types import SimpleNamespace


def test_observer_round_trips_through_config_and_runtime():
    from agent_core.progress import (
        ModelStartEvent,
        emit_progress,
        get_progress_observer_from_config,
        get_progress_observer_from_runtime,
        set_progress_observer,
    )

    events = []
    config = {"configurable": {"thread_id": "thread-1"}}
    observer = SimpleNamespace(emit=lambda event: events.append(event))

    updated = set_progress_observer(config, observer)

    assert updated is not config
    assert updated["configurable"]["thread_id"] == "thread-1"
    assert get_progress_observer_from_config(updated) is observer

    runtime = SimpleNamespace(config=updated)
    assert get_progress_observer_from_runtime(runtime) is observer

    emit_progress(observer, ModelStartEvent(thread_id="thread-1"))
    assert events == [ModelStartEvent(thread_id="thread-1")]


def test_emit_progress_swallows_observer_errors(caplog):
    import logging

    from agent_core.progress import ModelStartEvent, emit_progress

    class BrokenObserver:
        def emit(self, event):
            raise RuntimeError("render broke")

    with caplog.at_level(logging.DEBUG):
        emit_progress(BrokenObserver(), ModelStartEvent(thread_id="thread-1"))

    assert "Progress observer failed" in caplog.text


def test_mark_streamed_result_sets_private_marker_without_mutating_original():
    from agent_core.progress import has_streamed_output, mark_streamed_output

    result = {"messages": [{"role": "assistant", "content": "hello"}]}

    marked = mark_streamed_output(result)

    assert marked is not result
    assert marked["messages"] == result["messages"]
    assert has_streamed_output(marked) is True
    assert has_streamed_output(result) is False


def test_mark_streamed_result_handles_non_dict_values():
    from agent_core.progress import has_streamed_output, mark_streamed_output

    result = mark_streamed_output("hello")

    assert result == {"final_response": "hello", "__agent_cli_streamed_output__": True}
    assert has_streamed_output(result) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_agent_progress.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.progress'`.

- [ ] **Step 3: Implement core progress module**

Create `agent_core/progress.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, runtime_checkable


logger = logging.getLogger(__name__)

PROGRESS_OBSERVER_CONFIG_KEY = "progress_observer"
STREAMED_OUTPUT_MARKER = "__agent_cli_streamed_output__"


@runtime_checkable
class ProgressObserver(Protocol):
    def emit(self, event: "ProgressEvent") -> None:
        ...


@dataclass(frozen=True)
class ModelStartEvent:
    thread_id: str | None = None
    kind: Literal["model_start"] = "model_start"


@dataclass(frozen=True)
class TokenDeltaEvent:
    text: str
    thread_id: str | None = None
    kind: Literal["token_delta"] = "token_delta"


@dataclass(frozen=True)
class ToolStartEvent:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str = ""
    thread_id: str | None = None
    kind: Literal["tool_start"] = "tool_start"


@dataclass(frozen=True)
class ToolCompleteEvent:
    tool_name: str
    args: dict[str, Any]
    result: Any
    duration_ms: int
    tool_call_id: str = ""
    thread_id: str | None = None
    blocked: bool = False
    kind: Literal["tool_complete"] = "tool_complete"


@dataclass(frozen=True)
class ToolErrorEvent:
    tool_name: str
    args: dict[str, Any]
    result: Any
    duration_ms: int
    error_message: str
    tool_call_id: str = ""
    thread_id: str | None = None
    kind: Literal["tool_error"] = "tool_error"


@dataclass(frozen=True)
class TurnCompleteEvent:
    thread_id: str | None = None
    streamed_output: bool = False
    kind: Literal["turn_complete"] = "turn_complete"


@dataclass(frozen=True)
class FallbackEvent:
    reason: str
    thread_id: str | None = None
    kind: Literal["fallback"] = "fallback"


@dataclass(frozen=True)
class ProgressHiddenEvent:
    hidden_count: int
    thread_id: str | None = None
    kind: Literal["progress_hidden"] = "progress_hidden"


ProgressEvent = (
    ModelStartEvent
    | TokenDeltaEvent
    | ToolStartEvent
    | ToolCompleteEvent
    | ToolErrorEvent
    | TurnCompleteEvent
    | FallbackEvent
    | ProgressHiddenEvent
)


def emit_progress(observer: ProgressObserver | None, event: ProgressEvent) -> None:
    if observer is None:
        return
    try:
        observer.emit(event)
    except Exception:
        logger.debug("Progress observer failed while handling %s", event, exc_info=True)


def set_progress_observer(
    config: dict[str, Any] | None,
    observer: ProgressObserver | None,
) -> dict[str, Any] | None:
    if observer is None:
        return config
    updated = dict(config or {})
    configurable = dict(updated.get("configurable") or {})
    configurable[PROGRESS_OBSERVER_CONFIG_KEY] = observer
    updated["configurable"] = configurable
    return updated


def get_progress_observer_from_config(config: dict[str, Any] | None) -> ProgressObserver | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    observer = configurable.get(PROGRESS_OBSERVER_CONFIG_KEY)
    if observer is None:
        return None
    if hasattr(observer, "emit"):
        return observer
    return None


def get_progress_observer_from_runtime(runtime: Any | None) -> ProgressObserver | None:
    return get_progress_observer_from_config(getattr(runtime, "config", None))


def with_thread(event: ProgressEvent, thread_id: str | None) -> ProgressEvent:
    if thread_id is None or getattr(event, "thread_id", None):
        return event
    return replace(event, thread_id=thread_id)


def mark_streamed_output(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        updated = dict(result)
        updated[STREAMED_OUTPUT_MARKER] = True
        return updated
    return {"final_response": "" if result is None else str(result), STREAMED_OUTPUT_MARKER: True}


def has_streamed_output(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get(STREAMED_OUTPUT_MARKER))
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_agent_progress.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_core/progress.py tests/test_agent_progress.py
git commit -m "feat: add progress event contract"
```

Expected: commit succeeds.

## Task 2: Terminal Progress Renderer

**Files:**
- Create: `agent_cli/progress.py`
- Test: `tests/test_agent_cli_progress.py`

- [ ] **Step 1: Write failing tests for concise rendering**

Create `tests/test_agent_cli_progress.py`:

```python
from __future__ import annotations

import io


def test_terminal_observer_streams_tokens_and_marks_output():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ModelStartEvent, TokenDeltaEvent, TurnCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(ModelStartEvent(thread_id="t1"))
    observer.emit(TokenDeltaEvent("hello", thread_id="t1"))
    observer.emit(TokenDeltaEvent(" world", thread_id="t1"))
    observer.emit(TurnCompleteEvent(thread_id="t1", streamed_output=True))

    output = stream.getvalue()
    assert "waiting for model..." in output
    assert "hello world" in output
    assert observer.streamed_output is True


def test_terminal_observer_formats_terminal_tool_summary():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent, ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)
    result = {
        "stdout": "\\nfirst useful line\\nsecond line",
        "stderr": "",
        "exit_code": 0,
    }

    observer.emit(ToolStartEvent("terminal", {"command": "rg submit_message agent_cli"}, "call-1"))
    observer.emit(ToolCompleteEvent("terminal", {"command": "rg submit_message agent_cli"}, result, 1234, "call-1"))

    output = stream.getvalue()
    assert '> terminal: rg submit_message agent_cli' in output
    assert '< terminal done 1.2s' in output
    assert 'first useful line' in output


def test_terminal_observer_hides_write_file_content():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(
        ToolCompleteEvent(
            "write_file",
            {"path": "agent_cli/repl.py", "content": "secret long content"},
            "wrote 2000 bytes",
            5,
            "call-1",
        )
    )

    output = stream.getvalue()
    assert "agent_cli/repl.py" in output
    assert "secret long content" not in output


def test_terminal_observer_caps_progress_lines():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream, max_progress_lines=2)

    observer.emit(ToolStartEvent("terminal", {"command": "one"}, "1"))
    observer.emit(ToolStartEvent("terminal", {"command": "two"}, "2"))
    observer.emit(ToolStartEvent("terminal", {"command": "three"}, "3"))
    observer.emit(ToolStartEvent("terminal", {"command": "four"}, "4"))

    output = stream.getvalue()
    assert "> terminal: one" in output
    assert "> terminal: two" in output
    assert "... more progress hidden" in output
    assert "four" not in output
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_agent_cli_progress.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_cli.progress'`.

- [ ] **Step 3: Implement terminal observer**

Create `agent_cli/progress.py`:

```python
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from agent_core.progress import (
    FallbackEvent,
    ModelStartEvent,
    ProgressEvent,
    ProgressHiddenEvent,
    TokenDeltaEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TurnCompleteEvent,
)


DEFAULT_MAX_SUMMARY_CHARS = 160
DEFAULT_MAX_PROGRESS_LINES = 80


class TerminalProgressObserver:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        max_progress_lines: int = DEFAULT_MAX_PROGRESS_LINES,
    ) -> None:
        self.stream = stream or sys.stderr
        self.max_summary_chars = max_summary_chars
        self.max_progress_lines = max_progress_lines
        self.progress_lines = 0
        self.hidden_count = 0
        self.hidden_notice_printed = False
        self.streamed_output = False
        self._token_started = False

    def emit(self, event: ProgressEvent) -> None:
        if isinstance(event, ModelStartEvent):
            self._write_progress("waiting for model...")
        elif isinstance(event, TokenDeltaEvent):
            self._write_token(event.text)
        elif isinstance(event, ToolStartEvent):
            self._write_progress(f"> {event.tool_name}: {format_tool_args(event.tool_name, event.args, self.max_summary_chars)}")
        elif isinstance(event, ToolCompleteEvent):
            status = "blocked" if event.blocked else "done"
            self._write_progress(
                f"< {event.tool_name} {status} {_format_duration(event.duration_ms)}"
            )
            summary = summarize_tool_result(event.tool_name, event.args, event.result, self.max_summary_chars)
            if summary:
                self._write_progress(f"  {summary}")
        elif isinstance(event, ToolErrorEvent):
            reason = _truncate(event.error_message, self.max_summary_chars)
            self._write_progress(
                f"< {event.tool_name} failed {_format_duration(event.duration_ms)}: {reason}"
            )
        elif isinstance(event, TurnCompleteEvent):
            if event.streamed_output:
                self.streamed_output = True
            self.flush()
        elif isinstance(event, FallbackEvent):
            self._write_progress(f"waiting for model... ({event.reason})")
        elif isinstance(event, ProgressHiddenEvent):
            self._write_progress(f"... {event.hidden_count} more progress events hidden")

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:
            pass

    def _write_token(self, text: str) -> None:
        if not text:
            return
        if not self._token_started:
            self._token_started = True
            self.streamed_output = True
            self._raw_write("\\n")
        self._raw_write(text)
        self.flush()

    def _write_progress(self, line: str) -> None:
        if self.progress_lines >= self.max_progress_lines:
            self.hidden_count += 1
            if not self.hidden_notice_printed:
                self.hidden_notice_printed = True
                self._raw_write("... more progress hidden\\n")
            return
        self.progress_lines += 1
        self._raw_write(f"{line}\\n")
        self.flush()

    def _raw_write(self, text: str) -> None:
        self.stream.write(text)


def format_tool_args(tool_name: str, args: dict[str, Any], max_chars: int = DEFAULT_MAX_SUMMARY_CHARS) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "path", "query", "pattern", "session_id"):
        value = args.get(key)
        if value:
            return _truncate(str(value), max_chars)
    if tool_name in {"write_file", "patch"}:
        path = args.get("path") or args.get("file") or args.get("filename")
        if path:
            return _truncate(str(path), max_chars)
    compact = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return _truncate(compact, max_chars)


def summarize_tool_result(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> str:
    if tool_name in {"write_file", "patch"}:
        return _write_summary(args, result, max_chars)
    if tool_name == "terminal":
        return _terminal_summary(result, max_chars)
    if tool_name == "process":
        return _process_summary(result, max_chars)
    if tool_name in {"read_file", "search", "grep", "rg"}:
        return _first_informative_line(_result_text(result), max_chars)
    return ""


def _write_summary(args: dict[str, Any], result: Any, max_chars: int) -> str:
    path = args.get("path") or args.get("file") or args.get("filename")
    if path:
        return _truncate(f"updated {path}", max_chars)
    text = _first_informative_line(_result_text(result), max_chars)
    return text or "updated"


def _terminal_summary(result: Any, max_chars: int) -> str:
    if isinstance(result, dict):
        text = _first_informative_line(str(result.get("stdout") or ""), max_chars)
        if text:
            return text
        text = _first_informative_line(str(result.get("stderr") or ""), max_chars)
        if text:
            return text
        if "exit_code" in result:
            return f"exit_code={result.get('exit_code')}"
    return _first_informative_line(_result_text(result), max_chars)


def _process_summary(result: Any, max_chars: int) -> str:
    if isinstance(result, dict):
        for key in ("status", "session_id", "output"):
            value = result.get(key)
            if value:
                return _truncate(f"{key}={value}", max_chars)
    return _first_informative_line(_result_text(result), max_chars)


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content is not None:
        return str(content)
    return "" if result is None else str(result)


def _first_informative_line(text: str, max_chars: int) -> str:
    for line in str(text).splitlines():
        normalized = " ".join(line.split())
        if normalized:
            return _truncate(normalized, max_chars)
    normalized = " ".join(str(text).split())
    return _truncate(normalized, max_chars) if normalized else ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _format_duration(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"
```

- [ ] **Step 4: Run renderer tests**

Run:

```bash
pytest tests/test_agent_cli_progress.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_cli/progress.py tests/test_agent_cli_progress.py
git commit -m "feat: render concise cli progress"
```

Expected: commit succeeds.

## Task 3: Stream-First Runner With Invoke Fallback

**Files:**
- Modify: `agent_core/agent_runner.py`
- Test: `tests/test_agent_runner.py`

- [ ] **Step 1: Add failing runner tests**

Append to `tests/test_agent_runner.py`:

```python

def test_runner_streams_message_chunks_when_observer_present(monkeypatch):
    import agent_core.agent_runner as runner
    from agent_core.progress import TokenDeltaEvent, has_streamed_output

    events = []

    class Observer:
        def emit(self, event):
            events.append(event)

    class FakeAgent:
        def stream(self, input_data, config=None, stream_mode=None):
            assert input_data == {"messages": [{"role": "user", "content": "start"}]}
            yield {"messages": [{"type": "AIMessageChunk", "content": "hello"}]}
            yield {"messages": [{"role": "assistant", "content": "hello"}]}

        def invoke(self, input_data, config=None):
            raise AssertionError("invoke should not be used")

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        observer=Observer(),
    )

    assert result["messages"][-1]["content"] == "hello"
    assert has_streamed_output(result) is True
    token_events = [event for event in events if isinstance(event, TokenDeltaEvent)]
    assert [event.text for event in token_events] == ["hello"]


def test_runner_falls_back_to_invoke_when_stream_missing(monkeypatch):
    import agent_core.agent_runner as runner
    from agent_core.progress import FallbackEvent, has_streamed_output

    events = []

    class Observer:
        def emit(self, event):
            events.append(event)

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": [{"role": "assistant", "content": "ok"}]}

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        observer=Observer(),
    )

    assert result == {"messages": [{"role": "assistant", "content": "ok"}]}
    assert has_streamed_output(result) is False
    assert any(isinstance(event, FallbackEvent) for event in events)


def test_runner_streaming_exception_propagates(monkeypatch):
    import pytest

    import agent_core.agent_runner as runner

    class Observer:
        def emit(self, event):
            pass

    class FakeAgent:
        def stream(self, input_data, config=None, stream_mode=None):
            yield {"messages": [{"role": "assistant", "content": "partial"}]}
            raise RuntimeError("model failed")

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])

    with pytest.raises(RuntimeError, match="model failed"):
        runner.invoke_agent_with_terminal_notifications(
            FakeAgent(),
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
            observer=Observer(),
        )
```

- [ ] **Step 2: Run targeted tests to verify failure**

Run:

```bash
pytest tests/test_agent_runner.py::test_runner_streams_message_chunks_when_observer_present tests/test_agent_runner.py::test_runner_falls_back_to_invoke_when_stream_missing tests/test_agent_runner.py::test_runner_streaming_exception_propagates -v
```

Expected: FAIL because `invoke_agent_with_terminal_notifications()` does not accept `observer`.

- [ ] **Step 3: Implement stream-first invocation**

Modify `agent_core/agent_runner.py`:

```python
from __future__ import annotations

import logging
import os
from typing import Any

from agent_core.progress import (
    FallbackEvent,
    ModelStartEvent,
    ProgressObserver,
    TokenDeltaEvent,
    TurnCompleteEvent,
    emit_progress,
    mark_streamed_output,
    set_progress_observer,
)
from agent_core.session_context import RuntimeContext
from agent_core.terminal_lifecycle import (
    cleanup_task_resources_for_thread_id,
    terminal_execution_scope,
)
from agent_core.process_lifecycle import is_process_shutdown_requested
from agent_core.terminal_notifications import (
    drain_terminal_notifications_for_thread_id,
    format_terminal_notification_message,
)
```

Replace `_invoke_agent_turn(...)` with:

```python
def _invoke_agent_turn(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None,
    thread_id: str | None,
    *,
    cleanup_reason: str,
    observer: ProgressObserver | None = None,
) -> Any:
    try:
        with terminal_execution_scope(thread_id):
            return _invoke_or_stream_agent(agent, input_data, config, thread_id, observer)
    finally:
        if thread_id and per_turn_cleanup_enabled():
            try:
                cleanup_task_resources_for_thread_id(thread_id, reason=cleanup_reason)
            except Exception:
                logger.exception("Failed to run per-turn terminal cleanup for thread %s.", thread_id)
```

Add helpers below `_invoke_agent_turn(...)`:

```python
def _invoke_or_stream_agent(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None,
    thread_id: str | None,
    observer: ProgressObserver | None,
) -> Any:
    if observer is None:
        return agent.invoke(input_data, config)

    stream = getattr(agent, "stream", None)
    if not callable(stream):
        emit_progress(observer, FallbackEvent("stream unavailable", thread_id=thread_id))
        return agent.invoke(input_data, config)

    emit_progress(observer, ModelStartEvent(thread_id=thread_id))
    streamed_text = False
    final_result: Any = None
    stream_config = set_progress_observer(config, observer)

    for chunk in stream(input_data, stream_config):
        final_result = chunk
        for text in _extract_token_deltas(chunk):
            if not text:
                continue
            streamed_text = True
            emit_progress(observer, TokenDeltaEvent(text=text, thread_id=thread_id))

    emit_progress(observer, TurnCompleteEvent(thread_id=thread_id, streamed_output=streamed_text))
    if final_result is None:
        return mark_streamed_output({"messages": []}) if streamed_text else {"messages": []}
    return mark_streamed_output(final_result) if streamed_text else final_result


def _extract_token_deltas(chunk: Any) -> list[str]:
    messages = _chunk_messages(chunk)
    if messages:
        text_parts = []
        for message in messages:
            if _is_ai_chunk(message):
                text_parts.append(_message_content(message))
        return [part for part in text_parts if part]
    if _is_ai_chunk(chunk):
        text = _message_content(chunk)
        return [text] if text else []
    return []


def _chunk_messages(chunk: Any) -> list[Any]:
    if isinstance(chunk, dict):
        value = chunk.get("messages")
        if isinstance(value, list):
            return value
        if value is not None:
            return [value]
    value = getattr(chunk, "messages", None)
    if isinstance(value, list):
        return value
    if value is not None:
        return [value]
    return []


def _is_ai_chunk(message: Any) -> bool:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "")
        return role == "AIMessageChunk"
    name = message.__class__.__name__
    role = str(getattr(message, "type", "") or getattr(message, "role", ""))
    return name == "AIMessageChunk" or role == "AIMessageChunk"


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)
```

Update `invoke_agent_with_terminal_notifications(...)` signature and calls:

```python
def invoke_agent_with_terminal_notifications(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    max_auto_resumes: int | None = None,
    observer: ProgressObserver | None = None,
) -> Any:
```

Change both `_invoke_agent_turn(...)` calls to pass `observer=observer`.

- [ ] **Step 4: Run agent runner tests**

Run:

```bash
pytest tests/test_agent_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_core/agent_runner.py tests/test_agent_runner.py
git commit -m "feat: stream agent progress with fallback"
```

Expected: commit succeeds.

## Task 4: ToolBus Progress Events

**Files:**
- Modify: `agent_core/tool_bus_middleware.py`
- Test: `tests/test_tool_bus_middleware.py`

- [ ] **Step 1: Add failing ToolBus progress tests**

Append to `tests/test_tool_bus_middleware.py`:

```python

def test_tool_bus_emits_progress_events_from_runtime_observer():
    from agent_core.progress import ToolCompleteEvent, ToolStartEvent, set_progress_observer
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    events = []
    observer = SimpleNamespace(emit=lambda event: events.append(event))
    request = _request("terminal", args={"command": "pwd"}, tool_call_id="call-1")
    request.runtime = SimpleNamespace(
        config=set_progress_observer(
            {"configurable": {"thread_id": "thread-1"}},
            observer,
        )
    )

    result = ToolBusMiddleware().wrap_tool_call(
        request,
        lambda req: _message(tool="terminal", content="ok"),
    )

    assert result.content == "ok"
    assert isinstance(events[0], ToolStartEvent)
    assert events[0].tool_name == "terminal"
    assert events[0].args == {"command": "pwd"}
    assert events[0].thread_id == "thread-1"
    assert isinstance(events[1], ToolCompleteEvent)
    assert events[1].result.content == "ok"
    assert events[1].duration_ms >= 0


def test_tool_bus_emits_error_event_for_handler_exception():
    from agent_core.progress import ToolErrorEvent, set_progress_observer
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    events = []
    observer = SimpleNamespace(emit=lambda event: events.append(event))
    request = _request("terminal", args={"command": "bad"}, tool_call_id="call-1")
    request.runtime = SimpleNamespace(
        config=set_progress_observer(
            {"configurable": {"thread_id": "thread-1"}},
            observer,
        )
    )

    def handler(_request):
        raise RuntimeError("boom")

    result = ToolBusMiddleware().wrap_tool_call(request, handler)

    assert result.status == "error"
    assert any(isinstance(event, ToolErrorEvent) for event in events)
    error_event = [event for event in events if isinstance(event, ToolErrorEvent)][0]
    assert error_event.tool_name == "terminal"
    assert "boom" in error_event.error_message


def test_tool_bus_marks_pre_hook_blocked_as_blocked_complete_event():
    from agent_core.progress import ToolCompleteEvent, set_progress_observer
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    events = []
    observer = SimpleNamespace(emit=lambda event: events.append(event))
    request = _request("terminal", args={"command": "pwd"}, tool_call_id="call-1")
    request.runtime = SimpleNamespace(
        config=set_progress_observer(
            {"configurable": {"thread_id": "thread-1"}},
            observer,
        )
    )
    blocked = _message(tool="terminal", content="blocked", status="error")

    result = ToolBusMiddleware(
        hooks=ToolBusHooks(pre_tool_call=[lambda req: blocked])
    ).wrap_tool_call(request, lambda req: _message(tool="terminal", content="should not run"))

    assert result is blocked
    complete_events = [event for event in events if isinstance(event, ToolCompleteEvent)]
    assert len(complete_events) == 1
    assert complete_events[0].blocked is True
```

- [ ] **Step 2: Run targeted tests to verify failure**

Run:

```bash
pytest tests/test_tool_bus_middleware.py::test_tool_bus_emits_progress_events_from_runtime_observer tests/test_tool_bus_middleware.py::test_tool_bus_emits_error_event_for_handler_exception tests/test_tool_bus_middleware.py::test_tool_bus_marks_pre_hook_blocked_as_blocked_complete_event -v
```

Expected: FAIL because ToolBus does not emit progress events.

- [ ] **Step 3: Emit progress events in ToolBus**

Modify imports in `agent_core/tool_bus_middleware.py`:

```python
from agent_core.progress import (
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
    emit_progress,
    get_progress_observer_from_runtime,
)
from agent_core.session_context import RuntimeContext
```

After `_prepare_request(...)` succeeds in `wrap_tool_call(...)`, emit start:

```python
            bus_request, call_request = self._prepare_request(request)
            observer = get_progress_observer_from_runtime(bus_request.runtime)
            thread_id = RuntimeContext.from_runtime(bus_request.runtime).thread_id
            emit_progress(
                observer,
                ToolStartEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
```

Make the same addition in `awrap_tool_call(...)`.

Change `_finalize_blocked(...)` signature to accept `observer` and `thread_id`, and emit blocked completion before hooks:

```python
    def _finalize_blocked(
        self,
        bus_request: ToolBusRequest,
        result: ToolMessage,
        duration_ms: int,
        *,
        observer: Any = None,
        thread_id: str | None = None,
    ) -> ToolMessage:
        emit_progress(
            observer,
            ToolCompleteEvent(
                tool_name=bus_request.tool_name,
                args=bus_request.args,
                result=result,
                duration_ms=duration_ms,
                tool_call_id=bus_request.tool_call_id,
                thread_id=thread_id,
                blocked=True,
            ),
        )
        self._run_post_hooks(
            bus_request,
            ToolBusResult(result=result, duration_ms=duration_ms, error=None),
        )
        return result
```

Change `_finalize(...)` signature to accept `observer` and `thread_id`. Emit complete/error after transform/limit so summaries see final visible content:

```python
    def _finalize(
        self,
        bus_request: ToolBusRequest,
        result: ToolResponse,
        duration_ms: int,
        error: Exception | None,
        *,
        observer: Any = None,
        thread_id: str | None = None,
    ) -> ToolResponse:
        bus_result = ToolBusResult(result=result, duration_ms=duration_ms, error=error)
        self._run_post_hooks(bus_request, bus_result)

        visible_result = result
        if isinstance(result, ToolMessage):
            transformed = self._run_transform_hooks(bus_request, result)
            visible_result = self._limit_result(bus_request.spec, transformed)

        if error is None:
            emit_progress(
                observer,
                ToolCompleteEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    result=visible_result,
                    duration_ms=duration_ms,
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )
        else:
            emit_progress(
                observer,
                ToolErrorEvent(
                    tool_name=bus_request.tool_name,
                    args=bus_request.args,
                    result=visible_result,
                    duration_ms=duration_ms,
                    error_message=f"{type(error).__name__}: {error}",
                    tool_call_id=bus_request.tool_call_id,
                    thread_id=thread_id,
                ),
            )

        return visible_result
```

Update all `_finalize_blocked(...)` and `_finalize(...)` call sites in sync and async paths to pass `observer=observer, thread_id=thread_id`.

- [ ] **Step 4: Run ToolBus tests**

Run:

```bash
pytest tests/test_tool_bus_middleware.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent_core/tool_bus_middleware.py tests/test_tool_bus_middleware.py
git commit -m "feat: emit tool progress events"
```

Expected: commit succeeds.

## Task 5: CLI Wiring and Duplicate Output Prevention

**Files:**
- Modify: `agent_cli/repl.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_repl.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Add failing `AgentCLI` tests for observer wiring and duplicate suppression**

Append to `tests/test_agent_cli_repl.py`:

```python

def test_submit_message_passes_progress_observer_and_suppresses_streamed_output():
    from agent_core.progress import mark_streamed_output

    store = FakeStore()
    calls = []

    class Observer:
        def emit(self, event):
            pass

    observer = Observer()

    def fake_runner(agent, input_data, config, *, observer=None):
        calls.append((agent, input_data, config, observer))
        return mark_streamed_output(
            {"messages": [{"role": "assistant", "content": "streamed ok"}]}
        )

    cli = AgentCLI(
        session_store=store,
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
        progress_observer_factory=lambda: observer,
    )

    output = cli.submit_message("hello")

    assert output == ""
    assert cli.assistant_replies == ["streamed ok"]
    assert calls[0][3] is observer
    assert calls[0][2]["configurable"]["thread_id"] == "s1"


def test_submit_message_without_progress_observer_keeps_legacy_runner_signature():
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append((agent, input_data, config))
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
    )

    assert cli.submit_message("hello") == "ok"
    assert len(calls) == 1
```

- [ ] **Step 2: Run targeted tests to verify failure**

Run:

```bash
pytest tests/test_agent_cli_repl.py::test_submit_message_passes_progress_observer_and_suppresses_streamed_output tests/test_agent_cli_repl.py::test_submit_message_without_progress_observer_keeps_legacy_runner_signature -v
```

Expected: first test FAILS because `AgentCLI.__init__()` does not accept `progress_observer_factory`.

- [ ] **Step 3: Update `AgentCLI` runner wiring**

Modify imports in `agent_cli/repl.py`:

```python
from agent_core.progress import has_streamed_output
```

Update type aliases:

```python
Runner = Callable[..., Any]
ProgressObserverFactory = Callable[[], Any]
```

Update `AgentCLI.__init__(...)` parameters:

```python
        progress_observer_factory: ProgressObserverFactory | None = None,
```

Store it:

```python
        self.progress_observer_factory = progress_observer_factory
```

In `submit_message()`, replace the direct `self.runner(...)` call with:

```python
            observer = (
                self.progress_observer_factory()
                if self.progress_observer_factory is not None
                else None
            )
            config = {"configurable": {"thread_id": session_id}}
            if observer is None:
                result = self.runner(
                    self.agent,
                    {"messages": [{"role": "user", "content": processed}]},
                    config,
                )
            else:
                result = self.runner(
                    self.agent,
                    {"messages": [{"role": "user", "content": processed}]},
                    config,
                    observer=observer,
                )
```

After `output = latest_ai_text(result)`, update duplicate suppression:

```python
        if output:
            self.assistant_replies.append(output)
            if has_streamed_output(result):
                return ""
            return format_assistant_output(output, self.display_markdown)
        return output
```

Update `default_runner(...)` signature:

```python
def default_runner(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any],
    *,
    observer: Any = None,
) -> Any:
    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    return invoke_agent_with_terminal_notifications(
        agent,
        input_data,
        config,
        observer=observer,
    )
```

- [ ] **Step 4: Inject observer factory in `make_cli()`**

Add this helper near `make_cli(...)` in `agent_cli/main.py`:

```python
def default_progress_observer_factory():
    from agent_cli.progress import TerminalProgressObserver

    return TerminalProgressObserver()
```

Modify the `make_cli(...)` return block to pass:

```python
        progress_observer_factory=default_progress_observer_factory,
```

Keep the import inside the helper so config/doctor/session commands do not import progress rendering.

- [ ] **Step 5: Add main-level regression test**

Append to `tests/test_agent_cli_main.py`:

```python

def test_make_cli_configures_progress_observer_factory(monkeypatch, tmp_path):
    import agent_cli.main as main_mod

    store = main_mod.SessionStore(tmp_path / "cli.sqlite")
    args = SimpleNamespace(workdir=str(tmp_path), resume=None)
    settings = SimpleNamespace(
        model_name="model",
        default_title="New session",
        profile=None,
        cli_home=tmp_path,
        display_theme="default",
        display_markdown="render",
        cron_enabled=False,
        cron_interval_seconds=60,
    )

    cli = main_mod.make_cli(args=args, store=store, checkpointer="cp", settings=settings)

    observer = cli.progress_observer_factory()
    assert observer.__class__.__name__ == "TerminalProgressObserver"
```

If `tests/test_agent_cli_main.py` does not already import `SimpleNamespace`, add:

```python
from types import SimpleNamespace
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
pytest tests/test_agent_cli_repl.py tests/test_agent_cli_main.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add agent_cli/repl.py agent_cli/main.py tests/test_agent_cli_repl.py tests/test_agent_cli_main.py
git commit -m "feat: wire cli progress observer"
```

Expected: commit succeeds.

## Task 6: Scriptable Output Channel Verification

**Files:**
- Test: `tests/test_agent_cli_progress.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add tests for stderr default and stdout-safe streamed turns**

Append to `tests/test_agent_cli_progress.py`:

```python

def test_terminal_observer_defaults_to_stderr(monkeypatch):
    import io
    import sys

    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ModelStartEvent

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)

    observer = TerminalProgressObserver()
    observer.emit(ModelStartEvent(thread_id="thread-1"))

    assert "waiting for model..." in stderr.getvalue()
```

Append to `tests/test_agent_cli_repl.py`:

```python

def test_run_repl_does_not_print_duplicate_streamed_output(capsys):
    from agent_core.progress import mark_streamed_output

    class Prompt:
        def __init__(self):
            self.calls = 0

        def prompt(self, prompt_text):
            self.calls += 1
            if self.calls == 1:
                return "hello"
            raise EOFError

    def fake_runner(agent, input_data, config, *, observer=None):
        return mark_streamed_output(
            {"messages": [{"role": "assistant", "content": "already streamed"}]}
        )

    class Observer:
        def emit(self, event):
            pass

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
        prompt_session=Prompt(),
        show_banner=False,
        progress_observer_factory=Observer,
    )

    assert cli.run_repl() == 0
    captured = capsys.readouterr()
    assert "already streamed" not in captured.out
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
pytest tests/test_agent_cli_progress.py::test_terminal_observer_defaults_to_stderr tests/test_agent_cli_repl.py::test_run_repl_does_not_print_duplicate_streamed_output -v
```

Expected: PASS.

- [ ] **Step 3: Run focused progress suite**

Run:

```bash
pytest tests/test_agent_progress.py tests/test_agent_cli_progress.py tests/test_agent_runner.py tests/test_tool_bus_middleware.py tests/test_agent_cli_repl.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add tests/test_agent_cli_progress.py tests/test_agent_cli_repl.py
git commit -m "test: cover progress output channels"
```

Expected: commit succeeds.

## Task 7: Final Verification

**Files:**
- No planned source edits unless verification exposes a defect.

- [ ] **Step 1: Run full relevant test suite**

Run:

```bash
pytest tests/test_agent_progress.py tests/test_agent_cli_progress.py tests/test_agent_runner.py tests/test_tool_bus_middleware.py tests/test_agent_cli_repl.py tests/test_agent_cli_main.py -v
```

Expected: PASS.

- [ ] **Step 2: Run broader CLI/core regression tests**

Run:

```bash
pytest tests/test_agent_cli_*.py tests/test_agent_runner.py tests/test_tool_bus_middleware.py tests/test_policy_tool_bus_integration.py -v
```

Expected: PASS.

- [ ] **Step 3: Manual smoke test with fake no-network runner**

Run:

```bash
python - <<'PY'
from agent_cli.progress import TerminalProgressObserver
from agent_core.progress import ModelStartEvent, TokenDeltaEvent, ToolCompleteEvent, TurnCompleteEvent

observer = TerminalProgressObserver()
observer.emit(ModelStartEvent(thread_id="smoke"))
observer.emit(TokenDeltaEvent("streamed", thread_id="smoke"))
observer.emit(TokenDeltaEvent(" response\\n", thread_id="smoke"))
observer.emit(ToolCompleteEvent("terminal", {"command": "echo ok"}, {"stdout": "ok\\n", "exit_code": 0}, 12, "call-smoke"))
observer.emit(TurnCompleteEvent(thread_id="smoke", streamed_output=True))
PY
```

Expected: stderr shows `waiting for model...`, streamed response text, and concise terminal completion/summary lines. Command exits `0`.

- [ ] **Step 4: Check git status**

Run:

```bash
git status --short
```

Expected: no uncommitted changes.
