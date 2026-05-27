# Agent CLI CommandContext Design

Date: 2026-05-27

## Purpose

Agent CLI now has a working command registry. `commands.py` owns built-in command metadata, `agent_cli/command_handlers/` contains domain-specific handlers, and `AgentCLI.handle_command()` is a small dispatcher.

The next improvement is to tighten the boundary between command handlers and the `AgentCLI` runtime. Today handlers still receive the whole `AgentCLI` object and can directly access internal helpers such as `_set_session()` and `_require_background_registry()`. That works, but it keeps handler modules coupled to `AgentCLI` internals.

This design introduces a thin `CommandContext` facade. It keeps the current runtime model intact while giving handlers a stable, explicit dependency surface.

## Goals

- Replace handler signatures from `handler(cli, arg, command)` to `handler(ctx, arg, command)`.
- Make `CommandContext` the only object passed to handlers.
- Keep `AgentCLI` as the owner of runtime state.
- Keep `AgentCLI.handle_command()` thin.
- Preserve current command behavior.
- Avoid introducing a larger command framework in this phase.

## Non-Goals

- Do not introduce `CommandResult`.
- Do not move all session/background/config logic into service objects.
- Do not change command metadata semantics in `commands.py`.
- Do not change help, completion, or slash command output.
- Do not move REPL lifecycle behavior such as prompt handling, background notification draining, or exit cleanup out of `AgentCLI`.
- Do not add new slash command functionality.

## Architecture

Add `agent_cli/command_context.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_cli.repl import AgentCLI


@dataclass
class CommandContext:
    cli: AgentCLI
```

`CommandContext` is a facade over `AgentCLI`; it does not own state. The first implementation should use property and method proxies to expose only stable operations needed by command handlers.

### Context API

Read-only runtime properties:

- `session_store`
- `checkpointer`
- `workdir`
- `model_name`
- `profile`
- `cli_home`
- `display_theme`
- `display_markdown`
- `session_id`
- `session`
- `assistant_replies`
- `last_user_message`
- `last_call_elapsed_seconds`
- `last_usage_metadata`

Stable operations:

- `ensure_session(first_message: str | None = None) -> str`
- `set_session(session_id: str) -> None`
- `submit_message(text: str) -> str`
- `effective_cli_home() -> Path | str`
- `require_background_registry()`
- `reload_runtime_settings() -> str`
- `skill_commands() -> dict[str, Any]`
- `skill_discovery() -> Any`
- `load_skill(command: Any) -> Any`

Implementation may delegate directly to the wrapped `AgentCLI`, for example:

```python
def set_session(self, session_id: str) -> None:
    self.cli._set_session(session_id)
```

This keeps private `AgentCLI` details contained in one module instead of scattered across handlers.

## Handler Registry Changes

Update handler type:

```python
CommandHandler = Callable[[CommandContext, str, CommandDef], str | None]
```

Update registry construction:

```python
self.command_context = CommandContext(self)
self.command_handlers = build_command_handlers(self.command_context)
```

`build_command_handlers()` should accept a `CommandContext`, not `AgentCLI`.

`AgentCLI.handle_command()` should dispatch with:

```python
return handler(self.command_context, arg, command)
```

Dynamic skill fallback remains in `AgentCLI.handle_command()` for this phase because it is an unresolved slash-command fallback, not a built-in command handler. It can use `self.command_context` internally if that reduces direct provider access.

## Handler Migration Rules

Handlers should:

- Receive `ctx`, not `cli`.
- Use `ctx.set_session()` instead of `cli._set_session()`.
- Use `ctx.require_background_registry()` instead of `cli._require_background_registry()`.
- Use `ctx.effective_cli_home()` instead of `cli._effective_cli_home()`.
- Use `ctx.submit_message()` instead of `cli.submit_message()` when retrying or invoking skills.
- Use `ctx.skill_discovery()` and `ctx.load_skill()` for skill command handling.

Handlers should not:

- Import `AgentCLI`.
- Access `cli._private_method` directly.
- Mutate runtime state through direct property assignment when a context method exists.

Temporary direct read access through context properties is acceptable. For example, `ctx.session_store.list_sessions()` and `ctx.assistant_replies` are fine in this phase.

## Data Flow

Command execution remains:

```text
raw input
 -> sanitize_terminal_input()
 -> resolve_command()
 -> dynamic skill fallback if unresolved
 -> handler = command_handlers[handler_key]
 -> handler(ctx, arg, command)
 -> str | None | EOFError
```

`CommandContext` does not catch or wrap exceptions. The existing REPL loop continues to print unexpected command errors as `Error: ...`, and `/exit` continues to rely on `EOFError`.

## Error Handling

No behavior change is intended.

- Unknown built-in command resolution still returns `Unknown command`.
- Missing handler still returns `Unhandled command: /<name>`.
- `/exit` still raises `EOFError`.
- Background registry absence still raises the existing runtime error through `ctx.require_background_registry()`.
- `/reload` still returns an error string rather than raising when config reload fails.

## Testing

Add `tests/test_agent_cli_command_context.py`:

- Context proxies core runtime properties.
- `ctx.ensure_session()` calls existing session creation behavior.
- `ctx.set_session()` updates current session.
- `ctx.submit_message()` delegates to `AgentCLI.submit_message()`.
- `ctx.require_background_registry()` returns configured registry and raises when missing.
- `ctx.reload_runtime_settings()` delegates to the CLI method.
- Skill provider and loader methods delegate through context.

Update existing tests:

- `tests/test_agent_cli_repl.py`
  - fake handler receives a `CommandContext`, not `AgentCLI`.
  - dynamic skill fallback still works.

- `tests/test_agent_cli_commands.py`
  - every built-in command handler key still resolves to a registered handler.

Suggested focused verification:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_command_context.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_commands.py -q
```

Full Agent CLI verification:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_*.py -q
```

## Acceptance Criteria

- `AgentCLI.handle_command()` remains a thin dispatcher.
- `AgentCLI` creates and owns one `CommandContext`.
- `build_command_handlers()` accepts `CommandContext`.
- All command handlers receive `ctx`.
- No command handler directly calls `cli._set_session()`, `cli._require_background_registry()`, or `cli._effective_cli_home()`.
- No command handler imports `AgentCLI`.
- Existing slash command behavior remains unchanged.
- Full Agent CLI test suite passes.
