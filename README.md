# LangChain Agent Workspace

## Runtime Layout

- `agent.py`: LangGraph entrypoint. Keep this file small because `langgraph.json` loads `agent` from here.
- `agent_core/`: agent construction, prompts, middleware helpers, model config, memory, shared schemas, and workspace path handling.
- `agent_tools/`: LangChain-facing tools plus internal tool implementation packages.
  - `public/`: preferred import location for LangChain tools exposed to agents.
  - `shared/`: first-party helper modules used by tool wrappers.
  - `file_toolkit/`: internal workspace file operation implementation.
  - `hermes_terminal_toolkit/`: imported Hermes terminal toolkit implementation.
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

## Hermes Terminal Session Contract

The project exposes two shell-related tools:

- `terminal`: first-class Hermes terminal tool for foreground and background commands. Use `terminal(background=False)` for foreground shell commands.
- `process`: first-class Hermes process tool for background process polling, logs, waiting, stdin, and killing.

Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, terminal tools hash that thread id into a path-safe Hermes `task_id`.
- If `execution_info.thread_id` is unavailable, tools fall back to `runtime.config["configurable"]["thread_id"]`.
- The raw thread id is not exposed to the model and is not written into Hermes paths or checkpoints.
- If no runtime thread id is available, tools fall back to the Hermes `default` task id. This fallback is intended for local tests and direct implementation calls only.
- Production callers should provide a stable LangGraph `thread_id` for each user conversation/session.

`terminal` and `process` do not expose `task_id` in their tool schemas. `process` validates that `session_id` belongs to the current runtime-derived `task_id` before allowing `poll`, `log`, `wait`, `kill`, `write`, `submit`, or `close`.

Security and approval:

- `terminal` uses Hermes built-in command guards by calling Hermes with `force=False`.
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
- Per-turn terminal cleanup cleans non-persistent Hermes terminal environments for the runtime-derived task id.
- Persistent Hermes terminal environments are not cleaned at normal turn boundaries; they remain available until the persistent environment idle reaper cleans them.
- Set `HERMES_TERMINAL_PER_TURN_CLEANUP=false` to disable runner-managed per-turn terminal cleanup.
- Direct `agent.invoke(...)` callers are responsible for their own terminal execution scope, notification resume, interrupt, and cleanup lifecycle.
- Explicit user-session shutdown should call `end_terminal_session(thread_id)`. Lower-level cleanup helpers remain available as `cleanup_terminal_session_for_thread_id(thread_id)` and `cleanup_terminal_session_for_runtime(runtime)`.
- Explicit cleanup kills running processes for the session task id and cleans the active Hermes environment.
- Parent agent startup calls `recover_terminal_processes()`. Hermes can recover host-backed background processes as detached sessions after restart; sandbox-backed processes are skipped by Hermes because their in-sandbox PIDs are not meaningful after restart.
- Parent agent startup installs process-level SIGTERM and SIGHUP handlers once per Python process.
- On SIGTERM or SIGHUP, the handler marks every active `terminal_execution_scope(thread_id)` thread as interrupted, sleeps for `HERMES_SIGTERM_GRACE` seconds, then raises `KeyboardInterrupt` so the hosting runtime can terminate the run.
- `HERMES_SIGTERM_GRACE` defaults to `1.5`. Set it to `0` to skip the grace window, or increase it if the host routinely runs terminal commands with slow shutdown behavior.
- The grace window is intentionally present so foreground terminal poll loops can observe the interrupt and kill their subprocess groups before Python exits.
- At Python process exit, project shutdown cleanup calls `process_registry.kill_all()` before `cleanup_all_environments()`. This covers background Hermes sessions in addition to active terminal environments.

Background process governance:

- `terminal(background=True)` is allowed for dev servers, file watchers, and long-running jobs.
- Each runtime-derived `task_id` allows up to 3 running background processes by default.
- Set `HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK` to change the per-session running-process quota.
- Finished background sessions do not consume quota.
- Commands that look like long-lived servers or shell-level backgrounding must use `background=True`; Hermes rejects common foreground server/watch patterns.
- Long-lived servers may persist across normal turns and can be managed with `process(action="poll" | "log" | "wait" | "kill")`.

Embedding applications are responsible for terminal lifecycle events:

- Pass a stable `configurable.thread_id` for every user conversation/session.
- Prefer `invoke_agent_with_terminal_notifications(...)` over direct `agent.invoke(...)` when you want built-in terminal execution scoping, notification resumes, and per-turn cleanup.
- Wrap agent execution in `terminal_execution_scope(thread_id)` when new-message interrupt behavior is required. Multiple active executions for the same `thread_id` are tracked and interrupted together.
- When a new user message arrives for the same `thread_id`, call `interrupt_terminal_wait_for_thread_id(thread_id)` before replacing or resuming the run. Blocking `process(action="wait")` calls will then return early with Hermes interrupt status.
- When the user session is actually closed, call `end_terminal_session(thread_id)` to kill scoped background processes and clean the Hermes environment.
- Direct `agent.invoke(...)` callers still get process signal cleanup after `build_agent()` installs handlers, but they do not get per-turn cleanup, notification resume, or new-user-message interrupt unless they use the runner/helper APIs above.

Background completion notifications:

- Hermes queues background terminal completion and watch-pattern events in `process_registry.completion_queue` for commands using `notify_on_complete` or `watch_patterns`.
- Project code consumes those events with `agent_core.terminal_notifications.drain_terminal_notifications_for_thread_id(thread_id)`, which routes them through the same `configurable.thread_id` to Hermes `task_id` mapping used by `terminal` and `process`.
- Embedding applications that want automatic same-thread continuation should call `agent_core.agent_runner.invoke_agent_with_terminal_notifications(agent, input_data, config)`.
- The runner uses the same `configurable.thread_id`, wraps initial and resumed turns in `terminal_execution_scope(thread_id)`, and resumes only with terminal events mapped to the same Hermes `task_id`.
- Automatic continuation is bounded by `HERMES_TERMINAL_MAX_AUTO_RESUMES`, defaulting to 3. Notifications produced after the final allowed resume are left queued for a future invocation.
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

Cron output is saved under the configured Hermes home. `deliver="origin"` queues thread-scoped notifications that embedding applications can drain with `cron.notifications.drain_cron_notifications_for_thread_id(thread_id)`.

## Agent CLI

This repository includes a minimal local CLI for the LangGraph agent:

Chat and ask modes require the LangGraph SQLite checkpointer package
(`langgraph-checkpoint-sqlite`) because the MVP stores conversation state in
SQLite. `sessions` and `--help` do not require it. Install it in the project
environment with `python -m pip install langgraph-checkpoint-sqlite`.

```bash
python -m agent_cli
python -m agent_cli ask "Summarize this repository"
python -m agent_cli sessions
python -m agent_cli chat --resume <session_id>
```

The CLI stores LangGraph checkpoints and lightweight session metadata in
`~/.langchain-agent/cli.sqlite` by default. Set `AGENT_CLI_HOME` to place this
state elsewhere.

Inside chat, use `/help` to list slash commands. The MVP supports basic
approve/reject prompts for human-in-the-loop interrupts.
