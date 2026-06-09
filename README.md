# LangChain Agent Workspace

## Runtime Layout

- `agent.py`: LangGraph entrypoint. Keep this file small because `langgraph.json` loads `agent` from here.
- `agent_core/`: agent construction, prompts, middleware helpers, model config, memory, shared schemas, and workspace path handling.
- `agent_tools/`: LangChain-facing tools plus internal tool implementation packages.
  - `public/`: preferred import location for LangChain tools exposed to agents.
  - `shared/`: first-party helper modules used by tool wrappers.
  - `file_toolkit/`: internal workspace file operation implementation.
  - `terminal_toolkit/`: imported terminal toolkit implementation.
  - Top-level modules such as `file_tools.py` and `terminal_tools.py` are compatibility shims during migration.
- `agent_tools/file_toolkit/`: internal file operation implementation used by `agent_tools/file_tools.py`.
  - `file_tools.py`: JSON-returning primitive orchestration functions for read/write/patch/search. This is not a LangChain tool module.
  - `file_operations.py`: shell-backed low-level file operations.
  - `result_models.py`: result dataclasses shared by low-level operations and patch application.
  - `terminal_environment.py`: local shell execution backend used by file operations.
  - `file_state.py`: cross-agent read/write coordination and stale-write detection.
  - `patch_parser.py`: V4A patch parsing and application.
  - `fuzzy_match.py`: fuzzy replacement strategies and no-match hints.
  - `file_safety.py`, `binary_extensions.py`, `redact.py`, `tool_output_limits.py`: focused safety, binary detection, redaction, and output-limit helpers.
- `skills/`: local procedural skills loaded by `skills_list` and `skill_view`.
- `base/` and `projects/`: examples, notebooks, and experiments. These are not part of the agent runtime.

## Import Rules

- Runtime code should import through `agent_core.*` and `agent_tools.*`.
- Keep root-level Python files limited to entrypoints.
- Put new LangChain tool schemas close to the tool implementation unless they are shared across modules.
- Keep LangChain-facing file tool policy in `agent_tools/file_tools.py`; keep low-level file operation mechanics in `agent_tools/file_toolkit/`.
- Keep workspace path policy in `agent_tools/file_policy.py`; low-level toolkit modules should not know about project-specific workspace rules.
- Avoid adding new behavior to `agent_tools/general.py`; it exists only as a compatibility export layer.

### Code Execution Tool

`execute_code` lets the agent run a short local Python script that can call a constrained set of project tools through generated `hermes_tools.py` stubs. Use it when a task needs 3 or more tool calls, loops, filtering, batching, retries, or large intermediate results that should be compressed before returning to the model.

For a single simple operation, direct tools such as `read_file`, `search_files`, `terminal`, or `web_search` are preferred.

Stages 1-3 are local-only. Windows and non-local terminal backends return an unsupported-backend error. Script-side terminal calls are foreground-only: background processes, PTY interaction, completion notifications, and watch patterns are disabled.

`code_execution` is enabled by default for `dev` and `test` runtime profiles. It is not enabled by default for `hosted` or `prod`, but it can be explicitly enabled through the `code_execution` toolset.

## terminal toolkit Terminal Session Contract

The project exposes two shell-related tools:

- `terminal`: first-class terminal toolkit tool for foreground and background commands. Use `terminal(background=False)` for foreground shell commands.
- `process`: first-class terminal process tool for background process polling, logs, waiting, stdin, and killing.

Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, terminal tools hash that thread id into a path-safe runtime `task_id`.
- If `execution_info.thread_id` is unavailable, tools fall back to `runtime.config["configurable"]["thread_id"]`.
- The raw thread id is not exposed to the model and is not written into runtime paths or checkpoints.
- If no runtime thread id is available, tools fall back to the runtime `default` task id. This fallback is intended for local tests and direct implementation calls only.
- Production callers should provide a stable LangGraph `thread_id` for each user conversation/session.

`terminal` and `process` do not expose `task_id` in their tool schemas. `process` validates that `session_id` belongs to the current runtime-derived `task_id` before allowing `poll`, `log`, `wait`, `kill`, `write`, `submit`, or `close`.

Security and approval:

- `terminal` uses terminal toolkit command guards by calling terminal toolkit with `force=False`.
- `terminal` and `process` are intercepted by the human-in-the-loop middleware before execution.

Layered permission model:

- `AGENT_RUNTIME_PROFILE` controls default runtime posture: `dev`, `test`, `hosted`, or `prod`.
- If `TERMINAL_ENV` is unset, `dev/test` default to `local` and `hosted/prod` default to `docker`.
- Read-only file tools run without human review when existing workspace admission allows them.
- Workspace-local `write_file` and `patch` calls run without review.
- Ordinary writes outside the workspace require one-shot approval; sensitive paths such as `~/.ssh`, `~/.aws`, `/etc`, and Docker socket paths are denied.
- Low-risk shell commands such as read-only file inspection, read-only Git commands, and tests run without review.
- Package installs, network commands, destructive commands, permission changes, background servers, and complex shell require one-shot approval.
- Hardline destructive commands are denied.
- In `hosted/prod`, Docker sandboxes default to no network. Approved network commands receive temporary network access for the command lifetime.

Lifecycle policy:

- `invoke_agent_with_terminal_notifications(...)` wraps each initial and notification-resume turn in `terminal_execution_scope(thread_id)` and runs per-turn terminal cleanup after each attempted turn, including error paths.
- Per-turn terminal cleanup cleans non-persistent terminal toolkit environments for the runtime-derived task id.
- Persistent terminal toolkit environments are not cleaned at normal turn boundaries; they remain available until the persistent environment idle reaper cleans them.
- Set `TERMINAL_PER_TURN_CLEANUP=false` to disable runner-managed per-turn terminal cleanup.
- Direct `agent.invoke(...)` callers are responsible for their own terminal execution scope, notification resume, interrupt, and cleanup lifecycle.
- Explicit user-session shutdown should call `end_terminal_session(thread_id)`. Lower-level cleanup helpers remain available as `cleanup_terminal_session_for_thread_id(thread_id)` and `cleanup_terminal_session_for_runtime(runtime)`.
- Explicit cleanup kills running processes for the session task id and cleans the active terminal toolkit environment.
- Parent agent startup calls `recover_terminal_processes()`. terminal toolkit can recover host-backed background processes as detached sessions after restart; sandbox-backed processes are skipped by terminal toolkit because their in-sandbox PIDs are not meaningful after restart.
- Parent agent startup installs process-level SIGTERM and SIGHUP handlers once per Python process.
- On SIGTERM or SIGHUP, the handler marks every active `terminal_execution_scope(thread_id)` thread as interrupted, sleeps for `TERMINAL_SIGTERM_GRACE` seconds, then raises `KeyboardInterrupt` so the hosting runtime can terminate the run.
- `TERMINAL_SIGTERM_GRACE` defaults to `1.5`. Set it to `0` to skip the grace window, or increase it if the host routinely runs terminal commands with slow shutdown behavior.
- The grace window is intentionally present so foreground terminal poll loops can observe the interrupt and kill their subprocess groups before Python exits.
- At Python process exit, project shutdown cleanup calls `process_registry.kill_all()` before `cleanup_all_environments()`. This covers background terminal sessions in addition to active terminal environments.

Background process governance:

- `terminal(background=True)` is allowed for dev servers, file watchers, and long-running jobs.
- Each runtime-derived `task_id` allows up to 3 running background processes by default.
- Set `TERMINAL_MAX_BACKGROUND_PROCESSES_PER_TASK` to change the per-session running-process quota.
- Finished background sessions do not consume quota.
- Commands that look like long-lived servers or shell-level backgrounding must use `background=True`; terminal toolkit rejects common foreground server/watch patterns.
- Long-lived servers may persist across normal turns and can be managed with `process(action="poll" | "log" | "wait" | "kill")`.

Embedding applications are responsible for terminal lifecycle events:

- Pass a stable `configurable.thread_id` for every user conversation/session.
- Prefer `invoke_agent_with_terminal_notifications(...)` over direct `agent.invoke(...)` when you want built-in terminal execution scoping, notification resumes, and per-turn cleanup.
- Wrap agent execution in `terminal_execution_scope(thread_id)` when new-message interrupt behavior is required. Multiple active executions for the same `thread_id` are tracked and interrupted together.
- When a new user message arrives for the same `thread_id`, call `interrupt_terminal_wait_for_thread_id(thread_id)` before replacing or resuming the run. Blocking `process(action="wait")` calls will then return early with terminal toolkit interrupt status.
- When the user session is actually closed, call `end_terminal_session(thread_id)` to kill scoped background processes and clean the terminal toolkit environment.
- Direct `agent.invoke(...)` callers still get process signal cleanup after `build_agent()` installs handlers, but they do not get per-turn cleanup, notification resume, or new-user-message interrupt unless they use the runner/helper APIs above.

Background completion notifications:

- terminal toolkit queues background terminal completion and watch-pattern events in `process_registry.completion_queue` for commands using `notify_on_complete` or `watch_patterns`.
- Project code consumes those events with `agent_core.terminal_notifications.drain_terminal_notifications_for_thread_id(thread_id)`, which routes them through the same `configurable.thread_id` to runtime `task_id` mapping used by `terminal` and `process`.
- Embedding applications that want automatic same-thread continuation should call `agent_core.agent_runner.invoke_agent_with_terminal_notifications(agent, input_data, config)`.
- The runner uses the same `configurable.thread_id`, wraps initial and resumed turns in `terminal_execution_scope(thread_id)`, and resumes only with terminal events mapped to the same runtime `task_id`.
- Automatic continuation is bounded by `TERMINAL_MAX_AUTO_RESUMES`, defaulting to 3. Notifications produced after the final allowed resume are left queued for a future invocation.
- CLI display, idle polling, websocket delivery, and push notifications are not implemented here. Future CLI or server code should build on the runner and notification helper APIs above.

## Cron Scheduling

Cron is not started automatically. Embedding applications that want scheduled jobs should call:

```python
from agent_core.cron_lifecycle import start_cron_scheduler, stop_cron_scheduler

start_cron_scheduler(interval_seconds=60)
```

To let the parent agent create and manage jobs, build it explicitly with:

```python
from agent_core.builders import build_agent

agent = build_agent(include_cron_tools=True)
```

Cron output is saved under the configured terminal toolkit home. The cron service also advances outbound delivery every tick: stale `delivering` events are recovered, and due `pending` or `failed` non-origin delivery events are retried even when no job is due. `deliver="origin"` queues thread-scoped notifications that embedding applications must drain with `cron.notifications.drain_cron_notifications_for_thread_id(thread_id)`. Inbound origin/platform polling remains a host responsibility; hosts that own pollers can call `cron.origin_poller.poll_deliveries(pollers, store=None, limit=100)` on their own cadence.

For local automatic scheduling, install and run the user-level cron service:

```bash
python -m agent_cli cron service install
python -m agent_cli cron service start
python -m agent_cli cron service status
python -m agent_cli cron service logs
```

Linux uses user systemd when it is available. macOS uses a user LaunchAgent. The installed service runs the existing foreground command:

```bash
python -m agent_cli cron serve --interval 60 --lease-seconds 180
```

Development installs keep the default in-process cron runner. For hosted or
production cron services, set `AGENT_RUNTIME_PROFILE=hosted` or
`AGENT_RUNTIME_PROFILE=prod` before installing the service; if
`AGENT_CRON_RUNNER_MODE` is not already set, the installed user service will use
`AGENT_CRON_RUNNER_MODE=subprocess`. This keeps the long-lived scheduler process
separate from each job's agent execution. If `AGENT_RUNTIME_PROFILE` is unset,
pass the production profile explicitly when installing:

```bash
python -m agent_cli --profile prod cron service install
```

If scheduled jobs are not firing, run:

```bash
python -m agent_cli cron doctor
```

`agent cron doctor` checks the effective profile, runner mode, subprocess
worker smoke, timeout configuration, and runner temp directory. It reports stale
runner temp directories by default. To explicitly remove stale runner temp
directories older than 24 hours, run:

```bash
python -m agent_cli cron doctor --cleanup-runner-tmp
```

If user-level services are unsupported on your platform, run the foreground scheduler directly:

```bash
python -m agent_cli cron serve
```

## Agent CLI

This repository includes a local terminal CLI for the LangGraph agent. It
supports interactive chat, one-shot questions, session resume, local health
checks, profile-specific state, skill commands, human approval decisions, and
in-process background tasks.

Chat and ask modes require the LangGraph SQLite checkpointer package
(`langgraph-checkpoint-sqlite`) because conversation state is stored in SQLite.
`sessions`, `doctor`, and `--help` do not need to start an agent model call.

```bash
python -m agent_cli
python -m agent_cli chat --resume <session_id>
python -m agent_cli ask "Summarize this repository"
python -m agent_cli sessions
python -m agent_cli doctor --workdir /home/miku/projects/langchain
python -m agent_cli config show
python -m agent_cli config get display.theme
python -m agent_cli config set display.markdown strip
```

Public options can be passed before or after a subcommand:

```bash
python -m agent_cli --profile dev doctor --workdir /repo
python -m agent_cli doctor --profile dev --workdir /repo
python -m agent_cli ask --model gpt-4.1 "hello"
python -m agent_cli config show --profile dev
```

Options:

- `--workdir <path>`: workspace directory for the CLI session.
- `--model <name>`: model display metadata for session lists and status.
- `--profile, -p <name>`: use `~/.langchain-agent/profiles/<name>` as CLI home
  unless `AGENT_CLI_HOME` is explicitly set.

State locations:

- Default CLI home: `~/.langchain-agent`
- Override: `AGENT_CLI_HOME=/path/to/home`
- SQLite database: `<cli_home>/cli.sqlite`
- Prompt history: `<cli_home>/history.txt`
- Logs: `<cli_home>/logs/agent.log` and `<cli_home>/logs/errors.log`

Optional `<cli_home>/config.yaml`:

```yaml
display:
  markdown: render
  theme: default
model:
  name: gpt-4.1
session:
  default_title: New session
```

Supported config keys:

- `display.markdown`: `render`, `strip`, or `raw` for live assistant output.
- `display.theme`: one of the built-in CLI themes.
- `model.name`: default model display/config value when `--model` is not passed.
- `session.default_title`: title used for new empty sessions.

Use `python -m agent_cli config show|get|set` to inspect and edit this file.
`config set` validates the whole schema before saving.

Inside chat, use `/help` to list slash commands.

Session commands:

- `/status` - Show session metadata, workdir, model, profile, storage paths, and message counts.
- `/title <name>` - Set session title.
- `/history [N]` - Show filtered user/assistant history.
- `/export <path.md>` - Export filtered history to Markdown.
- `/new` - Start a new session.
- `/resume <session_id>` - Resume a previous session.
- `/sessions` - List recent sessions.

Background commands:

- `/background <prompt>` - Start an in-process background task in a new session.
- `/tasks` - List recent background tasks.
- `/tasks all` - List a larger recent task history across states.
- `/tasks <task_id>` - Show task details and the session resume command.
- `/tail <task_id>` - Show the latest persisted result, error, and steer messages.
- `/queue` - Show active background work.
- `/steer <task_id> <message>` - Queue a steering message for a task.
- `/approve <task_id>` - Continue a task waiting for human approval.
- `/stop <task_id>` - Request cooperative stop.

Skill and utility commands:

- `/skills` - List available local skills and dynamic skill slash commands.
- `/skill <name>` - Show skill details.
- `/doctor` - Run local health checks.
- `/reload` - Reload `.env` and `config.yaml` without restarting the REPL.
- `/clear` - Clear the terminal.
- `/exit` - Exit the CLI.

Human approval prompts support approving, rejecting with a message, editing tool
arguments as JSON, responding to the agent, and approving or rejecting all
remaining requests.

`python -m agent_cli doctor` prints stable `OK`, `WARN`, and `FAIL` lines. WARN
does not make the command fail; any FAIL exits with code `1`.

Troubleshooting:

- Missing `langgraph-checkpoint-sqlite`: install it in the project environment.
- Missing `OPENAI_API_KEY`: `doctor` reports WARN; chat may still fail if the configured model requires OpenAI.
- Malformed `config.yaml`: `doctor` reports FAIL and chat startup returns code `2`.
- SQLite or CLI home not writable: choose another `AGENT_CLI_HOME` or fix permissions.
- Unknown session id: run `python -m agent_cli sessions` and retry with a listed id.
- Stale background task warnings: restart the CLI and inspect `/tasks all`; stale rows are metadata only unless a live worker exists.
- Background task completed: use `/tasks <task_id>` or `/resume <session_id>` from the notification.

## CLI Commands

The Agent CLI supports these slash commands:

### Session Commands
- `/status` - Show session metadata, workdir, model, profile, storage paths, and message counts.
- `/title <name>` - Set session title.
- `/history [N]` - Show filtered user/assistant history.
- `/export <path.md>` - Export filtered history to Markdown.
- `/new` - Start a new session.
- `/resume <session_id>` - Resume a previous session.
- `/sessions` - List recent sessions.
- `/retry` - Resubmit the last normal user message.

### Clipboard Commands
- `/copy [N]` - Copy the latest or Nth latest assistant response using OSC52.

### Background Commands
- `/background <prompt>` - Start an in-process background task in a new session.
- `/tasks` - List recent background tasks.
- `/tasks all` - List a larger recent task history across states.
- `/tasks <task_id>` - Show detailed task metadata and resume hint.
- `/tail <task_id>` - Show latest persisted result, error, and steer messages.
- `/queue` - Show active background work.
- `/steer <task_id> <message>` - Queue a steering message for a task.
- `/approve <task_id>` - Continue a task waiting for human approval.
- `/stop <task_id>` - Request cooperative stop.

### Navigation Commands
- `/new` - Start a new session.
- `/resume <session_id>` - Resume a previous session.
- `/sessions` - List recent sessions.

### Skill and Utility Commands
- `/skills` - List available local skills and dynamic skill slash commands.
- `/skill <name>` - Show skill details.
- `/usage` - Show local session usage, estimated tokens, checkpoint status, and last-call latency.
- `/doctor` - Run local health checks.
- `/reload` - Reload `.env` and `config.yaml`.
- `/clear` - Clear the terminal.
- `/exit` - Exit the CLI.

### Tab Completion
Type `/` followed by a partial command to see completions:
- Command names
- Session IDs (for `/resume`)
- Skill names (for `/skill`)
- File paths (for `/export`)

### Terminal Input Features

#### Bracketed Paste Support
The CLI handles bracketed paste mode sequences from terminals. Pastes with 5+ lines are automatically collapsed and saved to `<cli_home>/pastes/` to keep the prompt clean, then expanded again before the message is submitted.

#### File Drop Detection
When a file path is entered as input, the CLI detects it and formats it as `[User referenced file: <path>]` for better agent understanding. Image paths are referenced as text in this phase; the CLI does not send multimodal image payloads yet.

#### Input Sanitization
Terminal control sequences (CPR, DSR, bracketed paste wrappers) are automatically stripped from input.

#### Clipboard Copy
`/copy` uses OSC52, so terminal and multiplexer clipboard integration must allow OSC52 sequences.
