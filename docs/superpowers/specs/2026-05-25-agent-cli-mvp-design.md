# Agent CLI MVP Design

## Goal

Build a minimal, project-native CLI for the current LangGraph/LangChain agent.
The CLI should borrow CLI reference's useful structure, especially command
registration and session-oriented execution, without porting reference implementation's full
gateway, plugin, provider, TUI, and tool ecosystem.

The first version must be runnable with:

```bash
python -m agent_cli
python -m agent_cli.main
```

## Non-Goals

The MVP does not include:

- reference implementation prompt_toolkit application TUI.
- Skins, banners, spinner animations, or inline diff rendering.
- Messaging gateways such as Slack, Telegram, Discord, or WhatsApp.
- reference implementation plugins, toolset management, backup, doctor, setup wizard, or auth
  wizard.
- Voice, browser, MCP management UI, or provider-specific model picker flows.
- Full-text session search.
- Per-tool multi-item approval editing. MVP supports only approve/reject.

## Approach

Use a clean MVP implementation beside the copied reference implementation code. The current
reference implementation-derived files in `agent_cli/` are treated as reference material, not as
the runtime path. Move them under `agent_cli/_archived_reference/` or otherwise
exclude them from the MVP import path, then create small project-native modules.

This is preferable to incrementally patching the copied reference files because
they contain hundreds of references to reference-only modules and would pull in
large unrelated subsystems.

## File Layout

```text
agent_cli/
  __init__.py
  __main__.py
  main.py
  repl.py
  commands.py
  session_store.py
  checkpoints.py
  interrupts.py
  rendering.py
  paths.py
  _archived_reference/
    banner.py
    cli.py
    config.py
    main.py
    session.py
    session_context.py
    skill_commands.py
    skin_engine.py
```

### Module Responsibilities

`paths.py` resolves CLI home paths. It supports `AGENT_CLI_HOME` and defaults to
`~/.langchain-agent`.

`checkpoints.py` opens the SQLite database and constructs the LangGraph SQLite
checkpointer.

`session_store.py` owns CLI metadata only. It does not store the agent's
conversation state.

`commands.py` defines `CommandDef`, a small command registry, alias resolution,
and help text.

`repl.py` runs the plain terminal REPL and single-question flow.

`interrupts.py` extracts LangGraph interrupt payloads and maps terminal
approve/reject input to `Command(resume=...)`.

`rendering.py` contains small formatting helpers for final responses, session
lists, and approval summaries.

`main.py` is the argparse entrypoint.

`__main__.py` calls `main.main()` so `python -m agent_cli` works.

## CLI Surface

Top-level commands:

```bash
python -m agent_cli
python -m agent_cli chat
python -m agent_cli chat --resume <session_id>
python -m agent_cli ask "question"
python -m agent_cli sessions
```

REPL slash commands:

```text
/help
/new
/sessions
/resume <session_id>
/clear
/skills
/skill <name>
/exit
```

Aliases may be added for common commands, for example `/quit` for `/exit`.

## Persistence

Use a single SQLite database:

```text
~/.langchain-agent/cli.sqlite
```

The same database file contains:

- LangGraph checkpoint tables maintained by the official SQLite checkpointer.
- A CLI-owned metadata table for session listing and resume UX.

The agent state source of truth is LangGraph checkpointing, keyed by
`configurable.thread_id`.

### CLI Metadata Table

```sql
CREATE TABLE IF NOT EXISTS cli_sessions (
  session_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  workdir TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  last_message_preview TEXT
);
```

Metadata is updated when a session is created, resumed, or receives a user
message. The title can initially be derived from the first user message and
truncated.

## Agent Integration

The CLI must use the current project runtime instead of reference implementation `AIAgent`.

Update `agent_core.builders.build_agent` to accept an optional checkpointer:

```python
def build_agent(*, include_cron_tools: bool = False, checkpointer=None):
    ...
    return create_agent(..., checkpointer=checkpointer)
```

If the installed LangChain `create_agent` API uses a different persistence
parameter, implementation should inspect the local signature and adapt.

Every user turn uses:

```python
input_data = {"messages": [{"role": "user", "content": text}]}
config = {"configurable": {"thread_id": session_id}}
result = invoke_agent_with_terminal_notifications(agent, input_data, config)
```

The CLI should not call `agent.invoke(...)` directly because
`invoke_agent_with_terminal_notifications` already handles terminal execution
scope, per-turn cleanup, and terminal notification continuation.

## Session Flow

Starting a new chat creates a new `session_id`, stores metadata, and uses it as
the LangGraph `thread_id`.

Resuming a chat validates that the `session_id` exists in `cli_sessions`, then
reuses it as the LangGraph `thread_id`.

`/new` creates a fresh session and switches the REPL to it.

`/resume <session_id>` switches the REPL to an existing session.

`/sessions` prints recent metadata rows, ordered by `updated_at DESC`.

## Interrupt Handling

The MVP supports basic approve/reject for LangGraph human-in-the-loop
interrupts.

When a result contains `__interrupt__`, the CLI prints a compact approval
summary and prompts:

```text
Approve? [y/N]:
```

If the user approves:

```python
Command(resume={"decisions": [{"type": "approve"} for _ in requests]})
```

If the user rejects:

```python
Command(
    resume={
        "decisions": [
            {"type": "reject", "message": "Rejected by user."}
            for _ in requests
        ]
    }
)
```

If the interrupt payload shape is unfamiliar, the CLI should display a safe
summary and reject by default unless the user explicitly approves.

The resume call uses the same `configurable.thread_id`.

## Skills Commands

`/skills` and `/skill <name>` use existing project skill tooling in
`agent_tools.public.skills`, notreference implementation `tools.skills_tool`.

The MVP can call the underlying metadata helpers if they remain internal, or
add a small public helper that returns skill metadata without requiring a
LangChain tool invocation.

## Output Rendering

The MVP prints only:

- User-facing command results.
- Latest assistant response content.
- Interrupt summaries and approve/reject prompts.
- Session IDs on session creation/resume.

It does not render streaming tokens, tool progress, inline diffs, or rich
panels in the first version.

## Error Handling

Missing optional checkpoint dependency should produce a clear message naming
the required package and the command that failed.

Invalid `/resume` session IDs should not create a session implicitly.

Agent invocation errors should be printed with a concise error line and should
not corrupt session metadata. Metadata `updated_at` should only advance after
the CLI accepts the user message for processing.

Keyboard interrupt in the REPL should cancel the current input line or return
to the prompt. EOF exits cleanly.

## Testing

Unit tests:

- Command resolution and aliases.
- Session metadata create/list/resume/touch behavior.
- Interrupt approve/reject decision mapping.
- Path resolution with and without `AGENT_CLI_HOME`.

Integration/smoke tests:

- `python -m agent_cli --help`.
- `python -m agent_cli sessions` against a temporary `AGENT_CLI_HOME`.
- `build_agent(checkpointer=fake)` accepts and forwards the checkpointer, or
  gracefully adapts to the installed `create_agent` signature.

Manual verification:

- Start `python -m agent_cli`.
- Send a simple message.
- Run `/new`, `/sessions`, `/resume <id>`.
- Trigger a tool requiring review and approve/reject it.

## Task Breakdown

1. Move existing reference implementation-derived files into `agent_cli/_archived_reference/`.
2. Add `agent_cli/__init__.py` and `agent_cli/__main__.py`.
3. Implement `paths.py`.
4. Implement `checkpoints.py` with LangGraph SQLite checkpointer creation.
5. Modify `agent_core.builders.build_agent` to accept optional checkpointer.
6. Implement `session_store.py` and the `cli_sessions` table.
7. Implement minimal `commands.py`.
8. Implement `rendering.py`.
9. Implement `interrupts.py`.
10. Implement `repl.py` for chat and ask flows.
11. Implement `main.py` argparse dispatch.
12. Add focused tests.
13. Run smoke checks and document usage in `README.md` if needed.

## Open Implementation Notes

Before coding, inspect the installed versions of LangChain and LangGraph in the
local environment. In particular, verify:

- The import path and constructor behavior for the SQLite checkpointer.
- Whether `create_agent` accepts `checkpointer` directly.
- The result shape returned by the current LangGraph runtime for interrupts.

These are implementation details, not product scope questions. The MVP design
remains the same if small API adapters are needed.
