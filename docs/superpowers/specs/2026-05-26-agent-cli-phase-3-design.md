# Agent CLI Phase 3 Design

Date: 2026-05-26

## Goal

Add background task execution, queue/task inspection, steering, cooperative stop,
approval continuation, a startup banner, and lightweight display themes to the
project-native `agent_cli`.

Phase 3 keeps the current ordinary terminal REPL. It does not introduce a
full-screen TUI, daemon, persistent worker process, Hermes skin engine, or
Hermes approval panel.

The selected architecture is a concurrent in-process thread task registry:
each `/background <prompt>` creates an independent CLI session/LangGraph thread
and runs a worker thread inside the current CLI process.

## Non-Goals

- No background task survival after the CLI process exits.
- No daemon, pid registry, subprocess worker pool, or cross-process recovery.
- No forced Python thread termination.
- No full-screen TUI, spinner, inline diff renderer, or Hermes sudo panel.
- No runtime `/theme` switching.
- No port of Hermes `skin_engine.py`; themes are small CLI color presets.
- No model/API integration changes outside existing `agent_core` builders and
  runner calls.

## Current Context

Phase 1 and Phase 2 already provide:

- `python -m agent_cli` / `python -m agent_cli.main`
- `chat`, `ask`, `sessions`, and `doctor`
- PromptSession with slash completion
- `/status`, `/title`, `/history`, `/export`
- rich approve/reject/edit/respond handling for LangGraph HITL interrupts
- profile-aware CLI home, dotenv, and `config.yaml`
- skill slash commands
- LangGraph SQLite checkpointing as conversation state storage
- lightweight SQLite CLI metadata in `cli_sessions`

The project also already contains terminal/process lifecycle support:

- `agent_core.agent_runner.invoke_agent_with_terminal_notifications()` invokes
  the agent and automatically resumes the same LangGraph thread for Hermes
  terminal completion notifications.
- `agent_core.terminal_lifecycle.interrupt_terminal_wait_for_thread_id()` can
  interrupt blocking terminal/process waits for a specific LangGraph thread.
- `agent_core.terminal_notifications` routes Hermes terminal completion events
  by task/thread.

Phase 3 should reuse these project-native pieces instead of copying Hermes's
gateway, TUI, or long-running worker architecture.

## Architecture

Add focused modules:

- `agent_cli/background.py`
  - in-process background task registry
  - task state machine
  - worker thread lifecycle
  - steer queue handling
  - approval pause/resume coordination
  - completion notification queue
- `agent_cli/banner.py`
  - startup Boxed Console banner
  - narrow-terminal fallback
  - background task summary rendering
- `agent_cli/theme.py`
  - lightweight theme lookup
  - `default`, `mono`, and `slate` presets
  - small helpers for colored text and prompt style

Modify existing modules:

- `agent_cli/commands.py`
  - register `/background`, `/tasks`, `/queue`, `/steer`, `/stop`, and
    `/approve`
  - add `/agents` as an alias of `/tasks`
- `agent_cli/repl.py`
  - own command routing for background commands
  - display banner at `chat` startup
  - drain background completion notifications before returning to the prompt
  - keep PromptSession reads on the main thread only
- `agent_cli/session_store.py`
  - add lightweight task metadata tables
  - keep LangGraph checkpoints as the source of conversation history
- `agent_cli/config.py`
  - validate `display.theme`
  - keep `display.markdown` validation from Phase 2
- `agent_cli/doctor.py`
  - report theme/config validity through existing config checks
- `agent_cli/main.py`
  - pass runtime theme/profile/home metadata into `AgentCLI`

Do not change `agent_core.agent_runner` for MVP unless tests uncover a narrow
thread-safety issue. Background workers should call the same runner currently
used by foreground `submit_message()`.

## Background Task Flow

`/background <prompt>` flow:

1. Generate a `task_id`, for example `bg_<8 hex chars>`.
2. Create a new CLI session record using the prompt-derived title.
3. Insert `cli_background_tasks` metadata with status `queued`.
4. Start a worker thread and transition status to `running`.
5. Worker invokes the existing agent runner with:

   ```python
   {"messages": [{"role": "user", "content": prompt}]}
   {"configurable": {"thread_id": session_id}}
   ```

6. The existing terminal notification runner may auto-resume the same thread
   when Hermes terminal/process completion notifications arrive.
7. If the worker sees queued steer messages after a turn, it consumes them in
   order and invokes the same LangGraph thread again.
8. If the worker receives a HITL interrupt, it stores the interrupt payload,
   sets status `waiting_approval`, and waits for `/approve <task_id>`.
9. On completion, failure, stop, or approval-needed state, the registry pushes a
   small notification to a thread-safe queue for the REPL to print.

The foreground REPL remains usable while background workers run.

## State Machine

Task statuses:

- `queued`: metadata exists, worker has not started or is pending startup
- `running`: worker is actively invoking or processing queued steer
- `waiting_approval`: worker paused on a LangGraph HITL interrupt
- `stopping`: cooperative cancellation requested
- `stopped`: worker observed cancellation and exited without more resumes
- `completed`: task finished normally
- `failed`: task raised an exception

Allowed transitions:

```text
queued -> running
running -> waiting_approval
waiting_approval -> running
running -> stopping -> stopped
waiting_approval -> stopping -> stopped
running -> completed
running -> failed
```

`queued` is intentionally retained even though MVP starts workers immediately.
It keeps the model compatible with a future concurrency limit or worker pool.

## Commands

### `/background <prompt>`

Creates an independent background task and session, starts the worker, and
returns task/session identifiers.

Example:

```text
Started background task bg_8f3a21 · session 20260526_153012_ab12cd34
```

Missing prompt returns usage text.

### `/tasks`

Shows recent background tasks with:

- task id
- status
- title
- session id
- created/updated time
- pending steer count
- last result preview or last error

Default view shows active tasks and recent completed, failed, or stopped tasks.
Future arguments such as `/tasks all` may broaden the list, but MVP can keep
argument handling small.

### `/agents`

Alias for `/tasks`.

### `/queue`

MVP queue view for active task states:

- `queued`
- `running`
- `waiting_approval`
- `stopping`

Because Phase 3 allows concurrent background workers, `/queue` is not a serial
execution queue. It exists to preserve the Hermes-style mental model and give a
short active-work view.

### `/steer <task_id> <message>`

Adds a user steering message to a background task.

Behavior:

- `running`: message is queued and consumed after the current turn.
- `waiting_approval`: message is queued and consumed after approval resumes.
- `queued`: message is queued before worker execution.
- `completed`, `failed`, `stopped`: command returns a clear status error.
- unknown task id returns an error.

Steer messages run in the same LangGraph thread as the background task.

### `/stop <task_id>`

Cooperative stop:

1. Mark `cancel_requested`.
2. Set status `stopping` unless already terminal.
3. Call `interrupt_terminal_wait_for_thread_id(session_id)` to interrupt
   blocking terminal/process waits when possible.
4. Worker exits after the current invoke/resume boundary and transitions to
   `stopped`.

Repeated stop on a stopping task returns `already stopping`. Stop on a terminal
task is a no-op with a clear message.

### `/approve <task_id>`

Only valid for `waiting_approval` tasks.

The main REPL thread uses the Phase 2 approval helper to collect
approve/reject/edit/respond decisions, then wakes the worker with
`Command(resume=...)`. Background threads never prompt on stdin directly.

If approval collection is interrupted by EOF or KeyboardInterrupt, `/approve`
must not send a partial resume. The task remains in `waiting_approval` and the
user can retry `/approve <task_id>`.

## Metadata Storage

Add `cli_background_tasks`:

```sql
CREATE TABLE IF NOT EXISTS cli_background_tasks (
  task_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  prompt_preview TEXT,
  last_result_preview TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  pending_steer_count INTEGER NOT NULL DEFAULT 0
);
```

Add `cli_background_steers`:

```sql
CREATE TABLE IF NOT EXISTS cli_background_steers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
```

The CLI metadata store should not duplicate conversation messages. LangGraph
SQLite checkpoints remain the source of conversation state and `/history` /
`/export` data.

The steer table is useful even though workers are not restored after process
exit: it explains pending work after an abnormal shutdown and supports
approval-waiting tasks cleanly.

## In-Memory Registry

`BackgroundTaskRegistry` owns runtime-only state:

- task map
- worker thread references
- per-task wake/approval events
- interrupt payloads waiting for approval
- notification queue
- an `RLock` for status and map updates

Threading rules:

- Main thread owns PromptSession and all user input.
- Worker threads never read stdin.
- Worker threads should not print long output directly.
- Worker threads may log and push small notification objects to the registry.
- Each worker uses an independent LangGraph thread id equal to its CLI
  `session_id`.
- If the shared checkpointer connection is not thread-safe, each worker should
  create its own checkpointer handle against the same SQLite path.

## Notifications

The REPL drains background notifications before displaying the next prompt and
after command handling where practical.

Example messages:

```text
[background done] bg_8f3a21 · completed · session 20260526_153012_ab12cd34
[background failed] bg_a10d09 · failed · ValueError: ...
[background attention] bg_91c04a · waiting approval · run /approve bg_91c04a
[background stopped] bg_77bc20 · stopped
```

MVP does not immediately print full final answers from background tasks. Users
can inspect the linked session with `/resume <session_id>`, `/history`, and
`/export`.

## Banner

Use the selected Boxed Console direction.

Wide terminal example:

```text
┌ Agent CLI ─────────────────────────────┐
│ cwd      /home/miku/projects/langchain │
│ profile  dev        model default      │
│ home     ~/.langchain-agent/profiles/dev
│ session  20260526_ab12cd34             │
├ Commands ──────────────────────────────┤
│ /background /tasks /steer /stop /approve
│ /status /history /export /skills /doctor
├ Background ────────────────────────────┤
│ RUN 2   WAIT 1   QUEUED 0   DONE 5     │
└────────────────────────────────────────┘
```

Rules:

- Show banner once at `chat` startup after session creation.
- Include cwd, profile when available, CLI home, model, session id, core
  commands, and background status counts.
- Use a narrow fallback for terminals that cannot fit the boxed layout.
- Do not display an ASCII logo or two-column Hermes-style tool inventory.
- `/status` remains focused on the current foreground session.
- `/tasks` owns detailed background task display.

## Theme

Add `display.theme` to config:

```yaml
display:
  theme: default
```

Supported values:

- `default`: Boxed Console with small status colors.
- `mono`: no color; suitable for logs, CI, and low-color terminals.
- `slate`: cool-toned status colors inspired by Hermes slate, without adopting
  the Hermes skin engine.

Theme applies to:

- banner border/section labels/status counts
- prompt prefix
- background notification status labels
- task status rendering

Invalid theme values should be config validation errors for `chat` and `ask`.
`doctor` should report the config failure instead of crashing.

## CLI Exit

On `/exit` or Ctrl-D, if tasks are running, waiting for approval, queued, or
stopping:

1. Print a concise warning.
2. Request cooperative stop for active tasks.
3. Return from the REPL after best-effort signaling.

MVP does not need an `/exit!` force command. Because background tasks are
in-process threads, process exit may terminate active work after the best-effort
stop signal.

## Error Handling

Task failure:

- status becomes `failed`
- `last_error` stores a concise exception summary
- traceback is written to `logs/errors.log`
- notification queue receives a failed notification

Approval:

- `/approve` on a non-waiting task returns a status error.
- `/approve` on an unknown task returns an unknown task error.
- repeated interrupts return the task to `waiting_approval`.

Stop:

- unknown task returns an error.
- terminal states return no-op messages.
- `stopping` returns `already stopping`.

Steer:

- invalid task id returns an error.
- missing message returns usage text.
- terminal task states reject new steer messages.

Threading:

- registry lock protects in-memory task state.
- metadata updates use short SQLite transactions.
- prompt rendering and stdin access stay on the main thread.

## Testing

Automated tests should use fake runners/fake agents, not live model calls.

Add `tests/test_agent_cli_background.py`:

- task creation writes metadata
- successful worker transitions to `completed`
- runner exception transitions to `failed`
- cooperative stop transitions to `stopping` then `stopped`
- steer is queued and consumed in order
- interrupt result transitions to `waiting_approval`
- approve resume wakes the worker and continues same thread
- concurrent tasks receive distinct task ids and session ids

Extend `tests/test_agent_cli_commands.py`:

- registry includes `/background`, `/tasks`, `/queue`, `/steer`, `/stop`,
  `/approve`
- `/agents` resolves to `/tasks`
- built-in commands still beat dynamic skill commands

Extend `tests/test_agent_cli_repl.py`:

- `/background <prompt>` returns task/session identifiers
- `/tasks` renders task rows
- `/queue` renders active tasks
- `/steer` validates and queues messages
- `/stop` calls cooperative cancellation
- `/approve` invokes approval UI only for waiting tasks
- REPL drains notifications before prompt display

Add `tests/test_agent_cli_banner.py`:

- wide Boxed Console rendering
- narrow fallback rendering
- background summary counts
- theme lookup for `default`, `mono`, and `slate`

Extend config/main/doctor tests:

- `display.theme` is parsed into runtime settings
- invalid theme fails `chat`/`ask` startup
- invalid theme is reported by `doctor`
- `chat` startup prints banner with fake prompt session

Recommended focused test command:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_background.py \
  tests/test_agent_cli_banner.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_doctor.py \
  -q
```

Before finishing implementation, run the full Agent CLI suite.

## Manual Smoke Test

With a configured model/API environment:

```text
python -m agent_cli chat
/background say hello in one sentence
/tasks
/steer bg_xxxxxx add one more sentence
/stop bg_xxxxxx
/exit
```

For approval testing, use a prompt that triggers a policy-reviewed tool call:

```text
/background run a shell command that requires approval
/tasks
/approve bg_xxxxxx
```

Expected behavior: the task enters `waiting_approval`, approval is collected on
the main REPL thread, and the worker resumes the same session after approval.
