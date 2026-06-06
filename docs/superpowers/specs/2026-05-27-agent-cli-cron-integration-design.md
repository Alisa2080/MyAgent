# Agent CLI Cron Integration Design

Date: 2026-05-27

## Purpose

The repository already has a cron system with JSON-backed scheduled jobs,
a tick-driven scheduler, origin delivery notifications, and an optional
`cronjob` LangChain tool. The project-native `agent_cli` does not yet expose
that system in normal CLI usage.

This design makes cron a first-class Agent CLI capability while keeping the
existing cron implementation separate from CLI session and background task
state.

## Goals

- Enable cron by default in `agent_cli chat`, with `config.yaml` support for
  `cron.enabled: false`.
- Include the `cronjob` tool in the CLI parent agent so users can create and
  manage scheduled jobs with natural language.
- Add REPL slash commands for cron management.
- Add top-level `python -m agent_cli cron ...` commands for cron management and
  manual ticks.
- Reuse one command service layer for both slash commands and top-level
  argparse commands.
- Show cron completion notifications in the REPL and inject unread cron
  updates into the next user turn.
- Preserve the existing cron runner, job storage, output storage, scheduler,
  and notification queue semantics.

## Non-Goals

- Do not introduce a daemon, system service, external worker, or long-running
  process outside the current CLI process.
- Do not merge cron jobs into the background task registry. Background tasks
  are in-process and temporary; cron jobs are persistent scheduled work.
- Do not support reference implementation platform delivery targets such as Telegram, Discord,
  Signal, Slack, or `platform:chat_id`.
- Do not rewrite `cron/` scheduling, persistence, or runner internals.
- Do not make `ask`, `sessions`, `doctor`, or `config` start a scheduler
  thread.
- Do not port reference implementation's large CLI implementation style into this codebase.

## User-Facing Behavior

### Chat

`python -m agent_cli chat` starts the cron scheduler by default. The scheduler
ticks on `cron.interval_seconds`, defaulting to 60 seconds. On REPL exit, the
CLI stops the scheduler thread after stopping active background tasks.

If the user sets:

```yaml
cron:
  enabled: false
```

then `chat` does not start the scheduler. Cron management commands still work,
and `cron tick` can still run due jobs manually.

### Natural Language Cron Management

The CLI agent includes the `cronjob` tool. A user can ask the agent to create,
list, update, pause, resume, remove, or run scheduled jobs. The tool captures
the current LangGraph `thread_id` when creating an origin-delivered job, so
completed job output can return to the session that created it.

### REPL Commands

Add `/cron` as a built-in command:

```text
/cron
/cron list [--all]
/cron add "every 2h" "Check status" [--name NAME] [--deliver origin|local]
/cron create "0 9 * * *" "Write daily report" [--repeat N]
/cron edit <job_id> [--schedule S] [--prompt P] [--name NAME]
/cron edit <job_id> [--skill NAME] [--add-skill NAME]
/cron edit <job_id> [--remove-skill NAME] [--clear-skills]
/cron pause <job_id>
/cron resume <job_id>
/cron run <job_id>
/cron remove <job_id>
/cron status
/cron tick
```

Supported create and edit flags:

- `--name`
- `--deliver`, limited to `origin` or `local`
- `--repeat`
- `--skill`, repeatable
- `--add-skill`, edit only, repeatable
- `--remove-skill`, edit only, repeatable
- `--clear-skills`, edit only
- `--script`
- `--workdir`
- `--schedule`
- `--prompt`

`/cron add` and `/cron create` are aliases. `/cron remove`, `/cron rm`, and
`/cron delete` are aliases.

### Top-Level Commands

Add `cron` to the top-level CLI:

```text
python -m agent_cli cron
python -m agent_cli cron list [--all]
python -m agent_cli cron create|add <schedule> [prompt] [flags]
python -m agent_cli cron edit <job_id> [flags]
python -m agent_cli cron pause <job_id>
python -m agent_cli cron resume <job_id>
python -m agent_cli cron run <job_id>
python -m agent_cli cron remove|rm|delete <job_id>
python -m agent_cli cron status
python -m agent_cli cron tick
```

Top-level cron commands do not require SQLite checkpointing.

Because top-level commands do not have an active REPL session, they reject
`--deliver origin` in this phase. Their default delivery target is `local`.

`cron status` reports whether the scheduler is running in the current process,
plus cron storage paths and job counts. It does not imply a global daemon
exists.

`cron tick` runs due jobs once and exits. If no jobs are due, it exits 0. If
one or more jobs fail, it exits 1 and prints the failed job ids and errors.

## Configuration

Extend `agent_cli.config_schema`:

```python
@dataclass(frozen=True)
class CronConfig:
    enabled: bool = True
    interval_seconds: int = 60

@dataclass(frozen=True)
class AgentCLIConfig:
    display: DisplayConfig = DisplayConfig()
    model: ModelConfig = ModelConfig()
    session: SessionConfig = SessionConfig()
    cron: CronConfig = CronConfig()
```

Allowed config paths:

- `cron.enabled`: boolean, default `True`
- `cron.interval_seconds`: positive integer, default `60`

`config show`, `config get`, and `config set` must support these paths. Unknown
cron keys should fail validation like the existing schema.

Runtime settings produced by `settings_from_config()` should include cron
settings so `make_cli()` can pass them into `AgentCLI`.

## Architecture

### Existing Cron Modules

Keep these modules as the lower-level cron system:

- `cron.jobs`: job schema, persistence, CRUD, schedule parsing.
- `cron.scheduler`: due job discovery, tick locking, run dispatch, origin
  delivery queueing.
- `cron.runner`: unattended cron job execution.
- `cron.notifications`: thread-scoped cron notification queue and formatting.
- `agent_core.cron_lifecycle`: scheduler thread lifecycle.
- `agent_tools.public.cronjob`: LangChain tool wrapper and job validation.

`agent_tools.public.cronjob` should expose a small non-tool helper for CLI use,
for example:

```python
def run_cronjob_action(
    action: str,
    *,
    origin_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    ...
```

The LangChain `cronjob` tool and `agent_cli.cron_commands` should both call
this helper. The tool obtains `origin_thread_id` from `ToolRuntime`; the CLI
passes the active session id explicitly. This avoids faking LangChain runtime
objects in CLI command code and keeps validation shared.

### CLI Cron Command Service

Add `agent_cli/cron_commands.py` as the shared command service layer.

Responsibilities:

- Provide functions for list, create, edit, pause, resume, run, remove, status,
  and tick.
- Normalize command names and aliases.
- Reuse `agent_tools.public.cronjob.run_cronjob_action()` for operations that
  need the same validation as the agent tool.
- Bind `origin.thread_id` when a REPL command uses `deliver="origin"`.
- Reject origin delivery when no active session id is provided.
- Render stable terminal text for job lists, job details, status, and tick
  summaries.

The service should accept already-parsed values rather than raw command-line
strings. This keeps parsing in the slash and argparse adapters.

### REPL Slash Handler

Add `agent_cli/command_handlers/cron.py`.

Responsibilities:

- Register a `cron_handlers()` mapping for the `cron` command.
- Parse `/cron` arguments with `shlex.split()`.
- Convert parsed flags into calls to `agent_cli.cron_commands`.
- Return text to `AgentCLI.handle_command()` instead of printing directly.

Add `CommandDef("cron", "Manage scheduled cron jobs.", "Cron", args_hint="[subcommand]")`
to `COMMAND_REGISTRY`.

### Top-Level Argparse Adapter

Extend `agent_cli.main.build_parser()` with a `cron` subparser. The parser
should mirror the slash command feature set where practical.

Handling occurs before SQLite checkpointer creation, like `sessions`, `doctor`,
and `config`.

The top-level adapter calls `agent_cli.cron_commands` and prints the returned
text. It returns 0 for successful operations and nonzero for command or tick
failures.

### Agent Factory

Change the CLI default agent factory so the CLI parent agent includes cron
tools:

```python
return build_agent(include_cron_tools=True, checkpointer=checkpointer)
```

Keep `build_agent()` itself defaulting to `include_cron_tools=False`; non-CLI
embedders must still opt in explicitly.

Cron job runner agents continue to use `cron.runner.build_cron_tools()`, which
excludes the `cronjob` tool and prevents recursive scheduled job management.

### Scheduler Lifecycle

Extend `AgentCLI.__init__()` with:

- `cron_enabled: bool = True`
- `cron_interval_seconds: int = 60`

`AgentCLI.run_repl()` should:

1. Ensure a session exists.
2. Start the cron scheduler if enabled.
3. Run the normal REPL loop.
4. On exit, stop active background tasks.
5. Stop the cron scheduler if this CLI started it.

The lifecycle should avoid stopping a scheduler that was already running before
this `AgentCLI` instance started. `start_cron_scheduler()` returns `False` when
another scheduler is already running in process; the CLI should track that and
only call `stop_cron_scheduler()` when it started the thread.

Scheduler startup failures should be logged and shown as a warning, but should
not prevent the REPL from opening.

## Notification Flow

Cron result delivery already queues events by `thread_id`. The REPL should make
those events visible and then feed them back into the conversation.

Add `AgentCLI` state:

```python
self.pending_cron_events: list[dict[str, Any]] = []
```

At REPL idle points, drain `cron.notifications.drain_cron_notifications_for_thread_id(self.session_id)`.
For each drained event:

1. Print a short notification line with job name, job id, status, output path,
   and a short preview.
2. Append the event to `pending_cron_events`.

On the next `submit_message()`:

1. If `pending_cron_events` is non-empty, format them with
   `format_cron_notification_message(events)`.
2. Prepend or append the formatted cron update to the user message as a clearly
   labeled context block.
3. Clear `pending_cron_events` only after the runner call has been constructed.

This separates display from injection. Events are not lost simply because they
were shown to the user.

## Delivery Rules

- `local`: save output only; no session notification.
- `origin` from agent tool: allowed when runtime config contains a thread id.
- `origin` from `/cron`: allowed after `ctx.ensure_session()` and uses the
  active CLI session id.
- `origin` from top-level `agent_cli cron`: rejected in this phase.
- unsupported delivery values fail fast with a clear message.

For `/cron add` inside a REPL, the default delivery should be `origin`, because
the command is session-scoped. For top-level `cron create`, the default delivery
should be `local`.

## Error Handling

- Invalid cron config returns exit code 2, matching existing config validation
  behavior.
- Invalid slash command arguments return usage text and do not raise tracebacks.
- Invalid top-level arguments are handled by argparse.
- `deliver=origin` without a session id returns a command error.
- Scheduler startup errors are logged and reported as warnings without blocking
  chat startup.
- `cron tick` prints failed jobs and exits 1 when any due job fails.
- Notification formatting errors fall back to a compact line using job id,
  status, and output path.

## Testing

Add or extend focused tests:

- `tests/test_agent_cli_config.py`
  - parses cron defaults;
  - parses configured `cron.enabled` and `cron.interval_seconds`;
  - rejects unknown cron keys and invalid intervals;
  - `config show/get/set` supports cron paths.
- `tests/test_agent_cli_builders.py` or `tests/test_cronjob_tool.py`
  - CLI default agent factory includes `cronjob`;
  - bare `build_agent()` still excludes `cronjob`.
- `tests/test_agent_cli_main.py`
  - `agent_cli cron list/status/tick` dispatches without creating a
    checkpointer;
  - top-level `--deliver origin` is rejected;
  - `ask`, `sessions`, and `doctor` do not start scheduler lifecycle.
- `tests/test_agent_cli_commands.py`
  - `/cron` is registered and appears in help;
  - `/cron` aliases resolve as expected.
- `tests/test_agent_cli_cron_commands.py`
  - create, edit, list, pause, resume, run, remove rendering;
  - REPL origin binding requires a session id;
  - top-level default delivery is local.
- `tests/test_agent_cli_repl.py`
  - `run_repl()` starts and stops scheduler when enabled;
  - disabled config skips scheduler lifecycle;
  - scheduler already running is not stopped by this CLI instance.
- `tests/test_cron_notifications.py` or a new CLI-focused test
  - drained cron events are printed and cached;
  - next submit injects cached cron update and clears it;
  - local delivery does not generate session notification.

Run the focused suite before implementation completion:

```bash
pytest \
  tests/test_agent_cli_config.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_repl.py \
  tests/test_cronjob_tool.py \
  tests/test_cron_lifecycle.py \
  tests/test_cron_notifications.py
```

## Acceptance Criteria

- `python -m agent_cli chat` starts cron by default and stops it on exit.
- `cron.enabled: false` disables scheduler startup in chat.
- CLI chat agent can create cron jobs using the `cronjob` tool.
- `/cron` can list, create, edit, pause, resume, run, remove, status, and tick.
- `python -m agent_cli cron ...` supports the same management operations where
  a session is not required.
- Origin-delivered cron results appear in the REPL and are included in the next
  user turn context.
- Top-level cron commands do not require SQLite checkpointing.
- Existing non-cron CLI commands keep their current behavior.
