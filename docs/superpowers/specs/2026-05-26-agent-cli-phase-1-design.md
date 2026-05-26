# Agent CLI Phase 1 Design

## Goal

Improve the current minimal `agent_cli` into a more usable local terminal CLI
without adopting the full Hermes TUI, gateway, plugin, background-task, or skin
systems.

Phase 1 focuses on:

- PromptSession-based interactive input.
- Slash command completion.
- Local session status and title editing.
- Human-readable history and Markdown export.
- Non-interactive doctor checks.
- Basic file logging for CLI lifecycle and errors.

The existing architecture remains intact: LangGraph checkpointing is the main
conversation state store, and the CLI metadata table remains a lightweight
session index.

## Non-Goals

- No full-screen TUI.
- No spinner, inline diff renderer, or approval panel.
- No background prompt execution, queueing, or steering.
- No Hermes gateway, plugin, skin, browser, voice, or multi-platform session
  context port.
- No network/model health check in `doctor`.
- No complete transcript duplication in the CLI metadata table.

## Dependencies

`prompt_toolkit` is a required CLI dependency for Phase 1.

`langgraph-checkpoint-sqlite` remains required for `chat` and `ask`, because
conversation state is stored through LangGraph SQLite checkpointing.

## Architecture

Keep the current small module layout and add focused helpers:

- `agent_cli/main.py`: argparse entrypoint, dotenv loading, logging setup,
  command routing.
- `agent_cli/repl.py`: REPL orchestration and command handling.
- `agent_cli/commands.py`: central slash command registry, aliases, help, and
  completion metadata.
- `agent_cli/input.py`: `prompt_toolkit.PromptSession` construction, history,
  and completer wiring.
- `agent_cli/history.py`: checkpoint-backed history loading and user/assistant
  message extraction.
- `agent_cli/doctor.py`: local environment and storage checks.
- `agent_cli/logging.py`: CLI log file setup.
- `agent_cli/rendering.py`: terminal and Markdown rendering helpers.
- `agent_cli/session_store.py`: lightweight session metadata.
- `agent_cli/checkpoints.py`: LangGraph SQLite checkpointer lifecycle.

Hermes reference files under `agent_cli/_hermes_reference` and
`agent_cli.backup-before-agent-cli-mvp-20260526` are treated as design
references only. Phase 1 borrows patterns, not source-level structure.

## PromptSession REPL

Replace direct `input()` usage with `prompt_toolkit.PromptSession`.

Behavior:

- Show the same simple `> ` prompt.
- Ctrl-D exits cleanly.
- Ctrl-C cancels the current input and returns to the prompt.
- Empty input is ignored.
- Slash-prefixed input is routed through `handle_command`.
- Other input is submitted to the agent as a user message.
- Prompt history persists under the CLI home directory.

The REPL should accept an injectable prompt/session adapter in tests so tests do
not need a real terminal.

## Slash Commands

Extend `CommandDef` with completion-oriented metadata while keeping
`resolve_command()` as the single command lookup path.

Phase 1 command set:

- `/help`
- `/new`
- `/sessions`
- `/resume <session_id>`
- `/status`
- `/title <name>`
- `/history`
- `/export <path.md>`
- `/clear`
- `/skills`
- `/skill <name>`
- `/exit`

Existing aliases continue to work. Help text and completion choices must derive
from the same registry.

## Completion

The completer provides:

- Slash command name completion from `COMMAND_REGISTRY`.
- `/resume` argument completion from the session metadata table.
- `/skill` argument completion from existing local skill metadata.
- File path completion for `/export`.

It does not implement Hermes dynamic model probing, plugin commands, gateway
commands, or `@file` context expansion.

## Session Status

`/status` prints local diagnostic context for the active CLI session:

- Current `session_id`.
- Session title.
- `created_at` and `updated_at`.
- Last message preview.
- Workdir.
- Model display metadata.
- CLI home path.
- SQLite DB path.
- Checkpointer status for the current process.

`/status` does not call the model, access the network, or perform expensive
health checks.

## Session Title

`/title <name>` updates the active session metadata title.

Rules:

- Empty argument returns usage text.
- The command requires an active session.
- The title is stored only in the metadata table.
- It does not rewrite LangGraph checkpoint state.

## History

`/history` reads the current session state from LangGraph checkpoint storage
using the current `thread_id`.

Only user and assistant messages are displayed. The renderer filters out:

- system messages
- tool messages
- middleware/internal messages
- interrupt payloads
- summaries unless they appear as ordinary assistant messages

Terminal format:

```text
User:
...

Assistant:
...
```

If no readable messages are found, show a clear message instead of failing.

## Export

`/export <path.md>` exports the same filtered user/assistant history used by
`/history`.

Rules:

- Output is Markdown.
- Relative paths resolve against the CLI workdir.
- Parent directories are created automatically.
- Tool/system/internal content is not exported.
- If there is no readable history, the command returns a clear message and does
  not create an empty transcript unless explicitly requested in a later phase.

Markdown format:

```markdown
# Session <session_id>

- Workdir: <workdir>
- Model: <model or unknown>
- Exported at: <timestamp>

## User

...

## Assistant

...
```

## Doctor

Add a non-interactive subcommand:

```bash
python -m agent_cli doctor
```

Checks:

- Python version.
- `prompt_toolkit` import.
- `langgraph-checkpoint-sqlite` import.
- CLI home creation and writability.
- SQLite DB openability.
- Workdir existence.
- `.env` loading path visibility.
- `OPENAI_API_KEY` presence, because the current project model config uses
  `ChatOpenAI`.

Doctor does not call the model and does not access the network.

Output uses simple status lines:

```text
OK    prompt_toolkit
WARN  OPENAI_API_KEY is not set
FAIL  SQLite DB cannot be opened: ...
```

Exit code:

- `0` when checks are OK or WARN only.
- `1` when any FAIL exists.

## Logging

Initialize CLI logging at startup.

Files:

- `~/.langchain-agent/logs/agent.log`
- `~/.langchain-agent/logs/errors.log`

Recorded events:

- CLI startup and shutdown.
- Command name, session id, and workdir.
- Message length for user prompts, not full prompt content.
- Agent invocation errors.
- Doctor check failures.

REPL errors continue to print to stderr and are also written to `errors.log`.
Logs must avoid storing full user prompts by default.

## Testing

Add or update tests for:

- PromptSession adapter behavior with fake prompt input.
- Command completion derived from `COMMAND_REGISTRY`.
- `/status` output.
- `/title` metadata update.
- `/history` filtering to user/assistant messages.
- `/export` Markdown writing.
- `doctor` WARN behavior for missing `OPENAI_API_KEY`.
- `doctor` FAIL exit code for storage failures.
- Logging setup creates log handlers/files.
- Existing CLI MVP tests continue to pass.

## Migration Notes

The implementation should avoid editing unrelated dirty files already present in
the working tree. Phase 1 should keep `python -m agent_cli`,
`python -m agent_cli chat`, `python -m agent_cli ask`, and
`python -m agent_cli sessions` behavior backward-compatible except for the
interactive input layer becoming PromptSession-based.
