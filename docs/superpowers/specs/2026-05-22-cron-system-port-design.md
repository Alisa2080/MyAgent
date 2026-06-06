# Cron System Port Design

Date: 2026-05-22

## Goal

Port the cron reference system into this LangChain agent project without bringing
over the Gateway Reference, platform adapters, CLI, REST API, or `AIAgent`
runtime.

The first implementation should provide:

- JSON-backed scheduled job storage.
- A tick-driven scheduler with explicit lifecycle APIs.
- A LangChain `cronjob` tool that can be optionally enabled on the parent
  agent.
- Cron job execution through this project's agent/tool stack.
- Local output persistence and thread-scoped notification delivery.

CLI development is explicitly out of scope for this design.

## Current Baseline

The repository already contains an untracked `cron/` directory copied from
Reference implementation. Those files preserve useful behavior, but still depend on reference-only
modules and concepts such as `cron_constants`, `archived_reference_time`, `cron_cli`,
`gateway.*`, `tools.*`, platform delivery adapters, and `run_agent.AIAgent`.

The current project boundaries are different:

- `agent_core/builders.py` constructs LangChain agents.
- `agent_core/agent_runner.py` manages terminal notification continuation.
- `agent_tools/public/*` exposes LangChain-facing tools.
- `agent_core/session_context.py` maps LangGraph `thread_id` values to reference implementation
  terminal task ids.
- The terminal toolkit is still part of this project, and already uses
  `TOOLKIT_HOME` as one of its home-directory inputs.

The port should preserve cron reference semantics where they remain useful, but
translate runtime integration into the current LangChain project shape.

## Approach

Use a project-native boundary split rather than directly patching the copied
reference files in place.

The copied behavior is the reference for scheduling, persistence, wake gates,
and at-most-once execution. The new integration replaces Gateway Reference,
platform adapter, registry, and `AIAgent` dependencies with current project
APIs.

## Module Boundaries

Create or adapt these modules:

- `cron/paths.py`: cron home, jobs file, output directory, scripts directory,
  and display helpers.
- `cron/jobs.py`: job schema, JSON persistence, CRUD, schedule parsing,
  next-run computation, due-job selection, state transitions, and output
  persistence.
- `cron/scheduler.py`: `tick()`, cross-process tick locking, due-job
  execution, before-run next-run advancement, workdir/non-workdir partitioning,
  and result marking.
- `cron/runner.py`: single-job execution, script wake gate, prompt
  construction, cron agent creation, timeout handling, and final-response
  extraction.
- `cron/notifications.py`: thread-scoped cron notification queues and drain
  helpers.
- `agent_core/cron_lifecycle.py`: explicit scheduler thread lifecycle.
- `agent_tools/public/cronjob.py`: LangChain `@tool("cronjob")` wrapper.
- `agent_core/builders.py`: optional `build_agent(include_cron_tools=False)`
  support.

This keeps storage, scheduling, execution, notification, lifecycle, and tool
surface independently testable.

## Storage Paths

Cron data follows the existing terminal home ecosystem.

Resolution should prioritize:

1. `TOOLKIT_HOME`, when set.
2. The current terminal toolkit home behavior when no `TOOLKIT_HOME` is
   configured.

Cron files live under the selected home:

```text
<home>/
  cron/
    jobs.json
    .tick.lock
    output/
      <job_id>/
        <timestamp>.md
  scripts/
    <user scripts>
```

Directories should be created with owner-only permissions where supported:

- directories: `0700`
- files: `0600`

`jobs.json` writes must remain atomic: write a temporary file in the target
directory, `json.dump`, flush, `fsync`, then atomic replace. A process-local
`threading.Lock` protects read-modify-write operations inside one Python
process.

## Job Schema

The first version keeps provider-compatible fields, but only some affect
execution.

Active fields:

- `id`
- `name`
- `prompt`
- `schedule`
- `schedule_display`
- `enabled`
- `state`
- `next_run_at`
- `last_run_at`
- `last_status`
- `last_error`
- `repeat`
- `deliver`
- `origin`
- `workdir`
- `script`
- `context_from`
- `skills`
- `skill`
- `enabled_toolsets`
- `created_at`

Compatibility fields stored but not honored by the first runner:

- `model`
- `provider`
- `base_url`

`origin` should store the LangGraph thread identity:

```json
{
  "thread_id": "conversation-thread-id"
}
```

The shape may preserve legacy `platform`, `chat_id`, and `chat_name` values
when present, but the first implementation does not depend on them.

## Schedule Parsing

Preserve the four reference implementation schedule forms:

- Duration one-shot: `30m`, `2h`, `1d`.
- Interval: `every 30m`, `every 2h`.
- Cron expression: `0 9 * * *`, using `croniter`.
- ISO datetime: `2026-02-03T14:00`.

Naive datetime values should be normalized to timezone-aware values using the
current local timezone. Stored timestamps should be ISO strings.

One-shot jobs default to `repeat.times = 1`. Recurring jobs default to
`repeat.times = null`, meaning forever.

## Tick Semantics

`cron.scheduler.tick(now=None)` is the core scheduler entry point and should
return a structured `TickResult`.

Tick behavior:

1. Acquire `<home>/cron/.tick.lock` using platform-appropriate file locking.
2. Load due enabled jobs with `next_run_at <= now`.
3. Apply a 120-second one-shot recovery window.
4. Apply dynamic recurring-job grace:
   - half the period;
   - clamped between 120 seconds and 7200 seconds.
5. Fast-forward recurring jobs that missed the grace window rather than
   immediately running stale work.
6. Advance `next_run_at` before execution to preserve at-most-once semantics.
7. Run jobs with `workdir` sequentially.
8. Run jobs without `workdir` in parallel, bounded by
   `AGENT_CRON_MAX_PARALLEL` when set.
9. Save output and mark each job run.

`mark_job_run()` updates:

- `last_run_at`
- `last_status`
- `last_error`
- `repeat.completed`
- `state`
- `enabled`

When repeat completion reaches `repeat.times`, the job becomes completed and is
disabled.

## Lifecycle API

Do not start cron automatically from `build_agent()`.

Add explicit lifecycle functions in `agent_core/cron_lifecycle.py`:

- `start_cron_scheduler(interval_seconds=60) -> bool`
- `stop_cron_scheduler(timeout=None) -> bool`
- `is_cron_scheduler_running() -> bool`

`start_cron_scheduler()` creates a daemon thread that calls
`cron.scheduler.tick()` on the configured interval. Starting twice must not
create duplicate ticker threads.

Embedding applications decide whether and when cron is active.

## Cron Tool Surface

Expose `cronjob` as an optional LangChain tool in
`agent_tools/public/cronjob.py`.

`build_agent(include_cron_tools=False)` remains false by default. When true,
the parent agent includes `cronjob`.

Supported actions:

- `create`
- `list`
- `update`
- `pause`
- `resume`
- `remove`
- `run`

On create, the tool captures `ToolRuntime.config["configurable"]["thread_id"]`
as `origin.thread_id` when available. If `deliver` is omitted, default to
`origin` when an origin exists, otherwise `local`.

The first implementation accepts only:

- `deliver="local"`
- `deliver="origin"`

Platform delivery targets such as `telegram:...` or `slack:...` should return
a clear unsupported-delivery error.

The tool keeps critical prompt scanning from reference implementation: block obvious prompt
injection, secret exfiltration, backdoor, and destructive patterns. It also
validates `context_from`, `script`, and `workdir` at create/update boundaries.

## Runner Agent

Cron jobs run through this project's LangChain agent stack, not reference implementation
`AIAgent`.

The runner builds a fresh cron agent per job using current model config:

- use `MAIN_MODEL` for the cron agent;
- use `SMALL_MODEL` where existing middleware requires it;
- ignore stored `model`, `provider`, and `base_url` overrides in the first
  version.

The cron system prompt should include:

- the job is unattended;
- do not ask clarifying questions;
- make a best effort with available context;
- do not create or modify cron jobs;
- final response is used for delivery;
- starting the final response with `[SILENT]` suppresses origin notification.

Prompt construction includes, in order:

1. Cron execution instructions.
2. Script output, when a script ran.
3. Most recent outputs from `context_from` jobs.
4. Loaded skill content from `skills`.
5. The job prompt.

## Tool Permissions

Cron is unattended, so its default tools are conservative.

Default cron tools:

- `list_directory`
- `search_files`
- `read_file`
- `file_info`
- `web_search`
- `web_fetch`
- `skills_list`
- `skill_view`

`enabled_toolsets` explicitly grants additional capability:

- `file_write`: `write_file`, `patch`
- `terminal`: `terminal`, `process`
- `delegation`: `task`

The first version does not include memory tools by default. If memory support
is added later, it should be a separate explicit toolset.

Cron agents must never include the `cronjob` tool. This prevents recursive
scheduled job creation.

## Workdir

`workdir` is supported in the first version.

Rules:

- must be an absolute path;
- `~` is expanded before validation;
- path must exist and be a directory at create/update time;
- invalid paths are rejected before storage.

At runtime, a job with `workdir` should:

- build prompt context from that directory;
- make file and terminal tools operate from that directory.

Because current tool working-directory behavior relies on process-level
environment in parts of the stack, jobs with `workdir` run sequentially. Jobs
without `workdir` may run in parallel.

## Script Wake Gate

`script` is included in the first version.

Rules:

- only relative paths are allowed;
- absolute paths, Windows drive-root paths, and `~` are rejected;
- resolved paths must stay inside the cron scripts directory;
- scripts run before the agent;
- timeout is controlled by `AGENT_CRON_SCRIPT_TIMEOUT`, defaulting to 120
  seconds.

Script stdout and stderr are captured, truncated to a bounded size, injected
into the prompt, and included in saved output.

Wake-gate behavior:

- If the last non-empty stdout line is JSON with `{"wakeAgent": false}`, skip
  the agent run.
- Save a normal output document explaining that the agent was skipped.
- Treat the run as successful.
- Use `[SILENT]` as the final response.

## Timeout

Use `AGENT_CRON_TIMEOUT`, defaulting to 600 seconds.

Reference implementation used an inactivity timeout based on `AIAgent` activity tracking. This
project's LangChain agent does not currently expose equivalent activity
signals, so the first version should implement a whole-agent invoke timeout.

If the future times out, mark the run as error and include a clear timeout
message in saved output.

## Notifications

`deliver="local"` saves output only.

`deliver="origin"` uses `origin.thread_id`:

- queue a thread-scoped event in `cron/notifications.py`;
- do not send platform messages;
- do not parse or resolve platform chat targets.

Notification event fields:

- `type`
- `job_id`
- `job_name`
- `status`
- `final_response`
- `output_path`
- `error`
- `run_at`

Expose:

- `queue_cron_notification(thread_id, event)`
- `drain_cron_notifications_for_thread_id(thread_id, max_events=10)`
- a formatting helper for embedding applications that want to feed cron
  updates back into an agent turn.

If the final response starts with `[SILENT]`, save output but do not queue an
origin notification.

## Error Handling

Storage failures should raise clear runtime errors.

Job execution failures should:

- save an output document when possible;
- set `last_status = "error"`;
- store a short `last_error`;
- keep recurring jobs scheduled according to the before-run advancement;
- complete or disable only when repeat policy requires it.

Unsupported delivery values should fail at tool create/update time. Existing
legacy jobs with unsupported delivery should run and save local output, then
record a delivery error rather than crashing the scheduler.

## Testing Plan

Unit tests should cover:

- schedule parsing for duration, interval, cron, and ISO datetime;
- next-run calculation;
- one-shot recovery window;
- recurring grace and fast-forward behavior;
- repeat completion and state transitions;
- atomic job save/load paths;
- path permissions where supported;
- malformed JSON handling;
- script containment validation;
- script timeout;
- `wakeAgent=false`;
- `context_from` output injection;
- skill content injection;
- `enabled_toolsets` mapping;
- cron agent never receiving `cronjob`;
- `workdir` validation;
- workdir sequential execution;
- non-workdir parallel execution and max parallel limit;
- `origin.thread_id` capture from `ToolRuntime`;
- notification queue and drain behavior;
- `[SILENT]` notification suppression;
- `build_agent(include_cron_tools=True/False)`;
- lifecycle idempotent start/stop;
- tick at-most-once advancement before execution.

Tests should avoid real network calls and real model calls by monkeypatching
agent creation and runner execution.

## Non-Goals

The first implementation does not include:

- CLI `/cron` commands;
- REST API;
- Gateway Reference integration;
- Telegram, Slack, Matrix, Discord, email, SMS, or webhook delivery;
- Matrix E2EE delivery;
- native media attachment parsing or sending;
- reference implementation `AIAgent`;
- reference implementation provider routing, credential pools, or `config.yaml` compatibility;
- per-job `model/provider/base_url` execution overrides;
- SQLite session storage;
- leader election beyond the tick file lock;
- a persistent service supervisor.

## Open Implementation Notes

The implementation should prefer small modules and narrow tests over editing a
large copied reference file until it imports. The copied `cron/` files can be used
as behavior references, but the final code should import through this project's
`agent_core.*` and `agent_tools.*` boundaries.

No implementation should begin until this design is reviewed and approved.
