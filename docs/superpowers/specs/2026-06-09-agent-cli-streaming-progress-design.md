# Agent CLI Streaming Progress Design

Date: 2026-06-09

## Goal

Make `python -m agent_cli` visibly responsive while an agent turn is running.
Today the CLI calls the agent synchronously and only prints the final assistant
response after the model, tools, and any auto-resume work have completed. Long
turns therefore look like a hung process and are hard to debug.

The first version adds default, concise progress output:

- stream assistant response text as it is generated;
- show tool start, completion, failure, and elapsed time;
- show short tool-result summaries based on tool type;
- keep final session, checkpoint, approval, and retry behavior compatible with
  the current CLI.

## Non-Goals

- Do not port the reference implementation's full-screen TUI, skin engine,
  status bar, voice mode, or provider framework.
- Do not show hidden model reasoning, private scratchpads, prompts, or raw
  internal state by default.
- Do not make verbose debugging the default mode.
- Do not change tool policy, human approval decisions, checkpoint storage
  format, or session metadata semantics.
- Do not call tools, read extra files, or access the network just to build
  summaries.

## Current Context

The relevant current flow is:

- `agent_cli/repl.py::AgentCLI.submit_message()` prepares user input, creates or
  reuses a session id, calls `self.runner(...)`, records metadata, then returns
  the latest assistant text.
- `agent_cli/repl.py::default_runner()` delegates to
  `agent_core.agent_runner.invoke_agent_with_terminal_notifications(...)`.
- `agent_core/agent_runner.py::_invoke_agent_turn()` calls `agent.invoke(...)`
  inside `terminal_execution_scope(...)`.
- `agent_core/builders.py::build_agent()` creates the LangChain agent and
  installs `ToolBusMiddleware`.
- `agent_core/tool_bus_middleware.py` already wraps tool execution and has
  pre/post/transform hooks. This is the right boundary for tool lifecycle
  progress.
- `agent_cli/rendering.py::latest_ai_text()` extracts the final assistant text
  from the returned agent result.

The reference implementation demonstrates useful patterns: streaming visible
assistant text, suppressing reasoning tags, showing concise tool progress, and
falling back to simple status output. This project should borrow those
behaviors without copying the monolithic CLI control flow.

## Selected Approach

Add a lightweight structured progress layer and wire it through the existing
runner and ToolBus boundaries.

This keeps responsibilities separate:

- CLI code renders progress.
- core runner code emits structured model/turn events.
- ToolBus emits structured tool lifecycle events.
- final result extraction, session storage, and checkpoint behavior remain
  unchanged.

## Architecture

### `agent_cli/progress.py`

Add a small module that defines progress events and terminal rendering.

Event types:

- `model_start`
- `token_delta`
- `tool_start`
- `tool_complete`
- `tool_error`
- `turn_complete`
- `fallback`
- `progress_hidden`

The module should expose:

- a `ProgressObserver` protocol with an `emit(event)` method;
- event dataclasses or typed dictionaries with stable fields;
- `TerminalProgressObserver`, the default concise renderer;
- pure helper functions for tool argument previews and result summaries.

The renderer owns terminal formatting only. It must not know how to invoke the
agent, mutate sessions, or execute tools.

### `agent_core/agent_runner.py`

Keep `invoke_agent_with_terminal_notifications(...)` compatible. Add optional
observer support either by:

- adding `observer: ProgressObserver | None = None` to the existing runner path;
  or
- adding a sibling `stream_agent_with_terminal_notifications(...)` and making
  the CLI default runner call it.

When an observer is present, the runner should prefer the agent's streaming or
event API and emit `token_delta` events for visible assistant text. If the agent
does not expose a usable stream API, the runner emits one `fallback` event and
uses the existing `agent.invoke(...)` path.

Terminal notification auto-resume remains part of the runner. Auto-resumed turns
should also emit progress through the same observer when streaming is available.

### `agent_core/builders.py` and ToolBus

Tool progress should be emitted from `ToolBusMiddleware`, because it already
sees normalized tool names, arguments, execution duration, blocked calls,
errors, transformed results, and result limiting.

Core code must not import `agent_cli`. The observer should be passed through
runtime/configurable state or an equivalent dependency-injection point. The
ToolBus hook should emit:

- `tool_start` before calling the underlying tool;
- `tool_complete` after a successful, blocked, or policy-returned ToolMessage;
- `tool_error` when a tool execution error is converted into a failure message.

Policy and human-in-the-loop behavior must stay authoritative. Progress events
describe what happened; they do not approve, block, retry, or transform calls.

### `agent_cli/repl.py`

`AgentCLI.submit_message()` creates a `TerminalProgressObserver` for interactive
turns and passes it to the runner. It still:

- prepares user text;
- preserves pending cron event behavior;
- creates/touches sessions only after successful fresh turns;
- captures usage metadata;
- extracts and stores the final assistant reply with `latest_ai_text(result)`.

If assistant text was already streamed, `submit_message()` should not return the
same final response for printing a second time. It should still store the reply
in `assistant_replies` so `/copy`, `/retry`, and usage-related commands keep
working.

## Default Output Rules

The first version defaults to concise progress.

### Model Text

- On model start, print one short status line such as `waiting for model...`.
- Stream visible assistant response text as token deltas arrive.
- Do not show hidden reasoning, scratchpads, raw prompts, or tool-call JSON as
  assistant prose.
- If streaming is unavailable, keep the status line and print the final answer
  through the existing non-streaming path.

### Tool Lifecycle

For each visible tool call, print short lifecycle lines:

```text
> terminal: rg "submit_message" agent_cli
< terminal done 1.2s
```

Failures include a compact reason:

```text
< terminal failed 0.4s: timeout
```

Repeated or high-volume progress should be capped per turn. After the cap is
reached, print one line such as:

```text
... more progress hidden
```

### Tool Result Summaries

Summaries are tool-type specific:

- `terminal`: first informative stdout/stderr line, exit code, or timeout.
- search/read tools: path, match count, line count, or first useful match.
- write/patch tools: changed paths and status, not full file content.
- `process` and background tools: session id, process status, and a small tail
  or state transition when available.
- unknown tools: tool name, success/failure, and elapsed time only.

Each summary should be short, with a default maximum around 160 characters.
Long or structured results are truncated deterministically.

## Output Channels

Progress output should not make scripted usage harder.

- Interactive `chat` shows progress by default.
- One-shot `ask` can show progress when attached to a TTY, but when stdout is
  not a TTY it should avoid contaminating stdout.
- Prefer stderr or an explicit renderer channel for progress so stdout can
  remain the final response channel in scriptable contexts.

## Error Handling and Fallbacks

Streaming unavailable:

- emit a single `fallback` event;
- call the existing `agent.invoke(...)` implementation;
- preserve final return values and session behavior.

Streaming agent/model error:

- preserve current exception propagation semantics;
- let existing CLI error handling and `/retry` behavior remain useful.

Observer/rendering error:

- catch and debug-log renderer failures;
- continue the agent turn;
- do not let terminal formatting failures break tool execution or model calls.

Tool summary failure:

- fall back to tool name, status, and elapsed time;
- do not re-run tools or inspect external state for a better summary.

## Testing Plan

### Progress Renderer Unit Tests

- token deltas are written and flushed correctly;
- streamed final text is marked to prevent duplicate printing;
- tool argument previews are stable and truncated;
- tool summaries differ by tool type;
- per-turn progress caps emit `... more progress hidden`;
- renderer exceptions are contained by observer-safe wrappers.

### Runner Unit Tests

- a fake streaming agent produces `model_start`, `token_delta`, and final result;
- a non-streaming fake agent falls back to `invoke`;
- streaming exceptions propagate like invoke exceptions;
- terminal notification auto-resume still runs and emits progress when possible;
- no observer preserves existing behavior.

### ToolBus Integration Tests

- successful tools emit start and complete events with duration;
- failed tools emit error status after conversion to a failure ToolMessage;
- policy-blocked tools emit visible completion or blocked status without
  bypassing approval;
- result summaries use transformed/limited ToolMessage content where available.

### `AgentCLI.submit_message()` Tests

- the runner receives observer/config for interactive turns;
- streamed turns store `assistant_replies` without returning duplicate text;
- non-streamed turns keep returning the final formatted response;
- runner exceptions still restore pending cron events;
- stdout/stderr behavior for scriptable `ask` is covered.

## Acceptance Criteria

- Running `python -m agent_cli` and submitting a long-running prompt shows
  visible progress before the final response.
- Assistant response text streams when the agent stream API is available.
- Tool calls show concise start, completion/failure, elapsed time, and
  type-aware summaries.
- Hidden reasoning is not displayed by default.
- If streaming is unavailable, the CLI still works through the existing invoke
  path.
- Existing session creation, checkpointing, `/copy`, `/retry`, cron event
  recovery, and approval behavior remain compatible.
- Tests cover renderer behavior, runner fallback, ToolBus event emission, and
  duplicate-output prevention.
