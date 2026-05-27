# Agent CLI Command, Config, and Background UX Design

Date: 2026-05-27

## Purpose

Agent CLI now has enough commands that `AgentCLI.handle_command()` is becoming a long dispatch chain. Config support also exists but is only partially schema-driven, and background tasks have a working in-process registry but limited inspection UX.

This design defines a staged improvement path:

1. Replace the long command if-chain with a handler registry.
2. Schema the CLI config and add config management commands.
3. Improve background task inspection and completion UX without daemonizing background work.

The stages are intentionally ordered. Command registry work is the foundation for adding config and background commands without growing `repl.py` further.

## Non-Goals

- Do not introduce a daemon, external worker process, or persistent scheduler for background tasks.
- Do not rewrite Agent CLI into a new command framework.
- Do not allow dynamic skill commands to override built-in commands.
- Do not make `config set` accept arbitrary unknown keys.
- Do not add full background output streaming in this phase; `/tail` starts from existing persisted task fields plus steer records.

## Stage 1: Command Registry

### Requirements

- Keep built-in command metadata in `agent_cli/commands.py`.
- Add `handler_key` to `CommandDef`, defaulting to the command name.
- Make `COMMAND_REGISTRY` the shared static source for help, completion, and dispatch.
- Split built-in command handlers by domain.
- Preserve existing command behavior.
- Keep dynamic skill command fallback after built-in resolution fails.

### Architecture

`CommandDef` will include:

```python
handler_key: str | None = None
```

The effective handler key is `handler_key or name`. Aliases only affect command resolution; they do not create separate handlers.

Add `agent_cli/command_handlers/`:

- `__init__.py`: exposes `build_command_handlers(cli)`.
- `session.py`: `new`, `sessions`, `resume`, `status`, `title`, `history`, `export`, `clear`, `retry`, `usage`.
- `background.py`: `background`, `tasks`, `queue`, `steer`, `stop`, `approve`.
- `skills.py`: `skills`, `skill`.
- `debug.py`: `help`, `doctor`, `exit`.
- `clipboard.py`: `copy`.

The first implementation should use a thin handler signature:

```python
CommandHandler = Callable[["AgentCLI", str, CommandDef], str | None]
```

This keeps the migration small and avoids introducing a larger `CommandContext` abstraction before there is a clear need for it.

### Dispatch Flow

`AgentCLI.handle_command()` should only:

1. Sanitize raw input.
2. Split command token and argument.
3. Resolve the command through `resolve_command()`.
4. If unresolved, attempt dynamic skill command fallback.
5. Find the handler by `command.handler_key`.
6. Invoke the handler.
7. Return `Unknown command` or `Unhandled command` for missing cases.

`/exit` continues to raise `EOFError`.

### Tests

- Every built-in `CommandDef` has a registered handler.
- `handle_command()` dispatches through a fake handler registry.
- Aliases resolve to the canonical command and handler.
- Dynamic skill fallback still works.
- Existing command behavior remains covered by current REPL tests.

## Stage 2: Config Schema and Commands

### Requirements

- Replace ad hoc config dict validation with an explicit lightweight schema.
- Avoid adding `pydantic` unless the project already depends on it.
- Add top-level config commands:
  - `agent_cli config show`
  - `agent_cli config get <path>`
  - `agent_cli config set <path> <value>`
- Add REPL `/reload` to reload `.env` and `config.yaml`.
- Make `display.markdown` affect assistant output rendering.
- Improve doctor config errors with path-specific messages.
- Add a checked-in config example file.

### Schema

Add `agent_cli/config_schema.py` with dataclass-backed schema objects:

- `AgentCLIConfig`
- `DisplayConfig`
- `ModelConfig`
- `SessionConfig`
- `ConfigValidationError(path, message)`
- `parse_config(data) -> AgentCLIConfig`
- `config_to_dict(config) -> dict`

Initial schema paths:

- `display.markdown`: one of `render`, `strip`, `raw`.
- `display.theme`: one of supported theme names.
- `model.name`: optional string.
- `session.default_title`: non-empty string.

Unknown keys should fail validation unless there is a deliberate compatibility reason to allow them. If compatibility requires tolerance, unknown keys must at least be reported by doctor as warnings.

### Top-Level CLI

Extend argparse with a `config` subcommand group:

```bash
agent_cli config show
agent_cli config get display.theme
agent_cli config set display.markdown strip
```

`config show` prints the normalized effective config in stable YAML or stable text format. `config get` prints only the requested value. `config set` writes to `<cli_home>/config.yaml` and validates the full file after mutation before saving.

`--profile` should select the target CLI home. `--workdir` and `--model` remain accepted for global option consistency, but `--model` must not write config unless the user explicitly runs `config set model.name ...`.

### Reload

Add `/reload` as a built-in command through the command registry.

Reload behavior:

- Reload `<cli_home>/.env` and project `.env`.
- Re-read and validate `config.yaml`.
- If valid, update:
  - `model_name`
  - `default_title`
  - `display_theme`
  - `display_markdown`
- If model-related config changes, clear `self._agent` so the next message rebuilds the agent.
- If invalid, keep the previous runtime settings and return an error.

### Markdown Output

Add `agent_cli/output_format.py`:

```python
format_assistant_output(text: str, mode: str) -> str
```

Mode behavior:

- `render`: keep current text behavior for now.
- `strip`: remove common Markdown markers with a lightweight formatter.
- `raw`: return text exactly as received.

Only live assistant display uses this setting. Session history and Markdown export continue storing and exporting the original transcript content.

### Doctor and Examples

Doctor should report config schema errors as:

```text
FAIL config display.markdown: must be one of raw, render, strip
```

Add a config example file, for example `agent_cli/config.example.yaml` or a docs-local equivalent, containing the supported keys.

### Tests

- Invalid `display.markdown` reports `display.markdown`.
- Invalid `display.theme` reports `display.theme`.
- `config set display.theme slate` can be read back by `config get`.
- `config set` rejects unknown paths.
- `/reload` updates runtime fields.
- `/reload` preserves old settings when new config is invalid.
- `display.markdown=strip` affects new assistant output but not stored history/export.
- Doctor uses path-specific schema errors.

## Stage 3: Background UX

### Requirements

- Keep the current in-process background registry.
- Do not daemonize background work.
- Improve task listing, task detail, tail inspection, completion notifications, and exit summaries.

### Command Behavior

`/tasks`

Show recent background tasks, defaulting to recent 20 tasks across active and recently terminal states. This preserves the current command while making completed tasks easier to find.

`/queue`

Continue showing active tasks for the current owner.

`/tasks all`

Show recent 100 tasks across owners and states. This is primarily for diagnostics and stale task inspection.

`/tasks <task_id>`

Show a detail page:

- task id
- status
- session id
- title
- created, updated, started, finished timestamps
- cancel requested
- pending steer count
- prompt preview
- last result preview
- last error

If the task has a session id, include:

```text
Resume with: /resume <session_id>
```

`/tail <task_id>`

Start as a lightweight inspection command using existing persisted state:

- last result preview
- last error
- recent steer records

No `--full` or streaming mode in this phase because the current store does not persist full output logs.

### Notifications

Background notifications should include actionable next steps:

- completed: `Completed <task_id> · session <session_id> · resume with /resume <session_id>`
- failed: `Failed <task_id> · session <session_id> · inspect with /tasks <task_id>`
- stopped: `Stopped <task_id> · session <session_id>`

### Exit Summary

`_stop_active_background_tasks_on_exit()` should clearly report:

- active tasks that received stop requests
- tasks that were already terminal
- tasks still active after the short join timeout
- a final hint to inspect history with `/tasks all`

The stop behavior should remain cooperative and should not block indefinitely.

### Store Support

Add small store helpers:

- `BackgroundTaskStore.get_steers(task_id, limit=20)`
- Optionally `BackgroundTaskStore.get_task_required(task_id)` to centralize missing task errors.

Do not add an output log table in this stage.

### Tests

- `/tasks`, `/tasks all`, and `/tasks <task_id>` each have command tests.
- `/tail <task_id>` covers result preview, error, and steer records.
- Completion notification includes `/resume <session_id>`.
- Exit summary handles active and terminal task states.
- Background state machine behavior is not changed.

## Implementation Strategy

Each stage should be its own implementation checkpoint and review boundary:

1. Registry refactor with no intentional behavior changes.
2. Config schema and config commands.
3. Background UX commands and formatting.

Prefer TDD for each stage:

- Add focused failing tests for the new behavior or refactor boundary.
- Implement the minimal changes.
- Run focused Agent CLI tests.
- Run the full Agent CLI test subset before each commit.

## Acceptance Criteria

- `AgentCLI.handle_command()` is a small dispatcher, not a growing if-chain.
- Help, completion, and dispatch share command metadata from `commands.py`.
- Config parsing has explicit schema errors with stable paths.
- `agent_cli config show|get|set` works against the selected profile home.
- `/reload` refreshes runtime settings without corrupting current state on failure.
- `display.markdown` has visible runtime effect.
- Background tasks can be inspected through `/tasks all`, `/tasks <task_id>`, and `/tail <task_id>`.
- Background completion and exit messages give actionable next commands.
