# Cron Run Observability and Timeout Schema Design

Date: 2026-05-30

## Context

The cron system already has the larger architectural pieces in place:

- Delivery is adapter-based, with a registry, dispatcher, and persisted delivery events.
- `agent cron serve` runs as a service with heartbeat/status JSON and a scheduler leader lease.
- Cron jobs, runs, and deliveries are persisted in SQLite through `StateStore`.
- Automatic scheduling now creates a run record before execution and advances `next_run_at` on completion.

The next gap is execution observability. A running cron job is still mostly a black box: the system can say that a job is running, but not what phase it is in, whether it is making progress, or why it was stopped. Timeout behavior is also still mostly global and duration-based, while the Hermes reference design treats inactivity as the healthier default signal.

This design covers two goals:

1. Run observability.
2. Job-level timeout schema, with idle timeout as the default behavior and max runtime as an optional hard cap.

## Goals

- Persist run activity fields so a cron run can be diagnosed while it is running and after it finishes.
- Add job-level timeout settings:
  - `idle_timeout_seconds`
  - `max_runtime_seconds`
- Preserve compatibility with `AGENT_CRON_TIMEOUT` by treating it as the default idle timeout.
- Make idle timeout the primary default. Long-running jobs should be allowed when they continue to make progress.
- Keep `max_runtime_seconds` opt-in.
- Record clear terminal reasons for timeout failures:
  - `idle_timeout`
  - `max_runtime_exceeded`
- Extend `cron status` with a concise summary plus Top N details for:
  - next due jobs
  - running jobs
  - stale running jobs
  - latest failed run
- Implement stage-level activity now, while preserving a stable interface for future Hermes-style fine-grained agent activity.

## Non-Goals

- Full Hermes-style tool/API/stream delta instrumentation inside the agent runtime.
- New delivery platforms such as Slack, Discord, or email.
- New run-management commands such as `cron logs`, `cron runs`, or delivery retry commands.
- Concurrency policy changes.
- Worker queue or backpressure changes.

## Current Constraints

`runs.exit_reason` already exists and should be reused. Existing values include `lease_expired`, `late_completion`, and `missed_run`.

The `runs` table does not yet store activity details. The design adds:

- `heartbeat_at TEXT`
- `last_activity_at TEXT`
- `last_activity_desc TEXT`
- `current_tool TEXT`

The `jobs` table does not yet store timeout settings. The design adds:

- `idle_timeout_seconds INTEGER`
- `max_runtime_seconds INTEGER`

The system has two execution modes:

- `inprocess`, via `cron.runner.run_job`
- `subprocess`, via `cron.runner_subprocess.run_job_subprocess`

The first implementation should keep both modes working. Stage-level activity can be updated from scheduler and parent-side runner loops before deeper agent instrumentation exists.

## Timeout Semantics

Idle timeout is the default health signal.

Resolution order for idle timeout:

1. Use job `idle_timeout_seconds` when set.
2. Otherwise use `AGENT_CRON_TIMEOUT`.
3. Otherwise use the current default of 600 seconds.
4. Values less than or equal to zero disable idle timeout.

Resolution order for max runtime:

1. Use job `max_runtime_seconds` when set.
2. Otherwise no hard runtime limit is enforced.
3. Values less than or equal to zero disable max runtime.

Idle timeout is measured from `runs.last_activity_at`. If that is empty, fall back to `started_at`, then `claimed_at`.

Max runtime is measured from `runs.started_at`. If `started_at` is empty, fall back to `claimed_at`.

When a timeout fires:

- terminate the running agent process where applicable
- mark the run failed
- set `runs.exit_reason` to `idle_timeout` or `max_runtime_exceeded`
- write an error message containing job id, run id, timeout limit, observed elapsed time, last activity description, and current tool when available
- release the job lease through the existing completion path

## Activity Model

This phase records stage-level activity. The activity interface should be generic enough for future fine-grained updates.

Required activity fields:

- `heartbeat_at`: the runner or scheduler is still alive
- `last_activity_at`: the job made meaningful progress
- `last_activity_desc`: a short stable description of the last meaningful progress
- `current_tool`: optional tool name or phase detail

Initial stage descriptions:

- `claimed`
- `started`
- `script_running`
- `agent_running`
- `delivery_pending`
- `delivery_dispatched`
- `completed`

Heartbeat and activity are intentionally separate. A heartbeat says the process is alive. Activity says useful progress happened. Idle timeout should use activity, not heartbeat.

The first implementation should expose a StateStore method such as:

```python
update_run_activity(
    run_id,
    *,
    heartbeat: bool = True,
    activity: bool = False,
    last_activity_desc: str | None = None,
    current_tool: str | None = None,
)
```

When `activity=True`, both `heartbeat_at` and `last_activity_at` are updated. When only `heartbeat=True`, only `heartbeat_at` changes.

## Scheduler Flow

The scheduler remains the owner of run state transitions.

Expected flow:

1. `claim_due_jobs()` creates a run and writes initial activity `claimed`.
2. `mark_run_started()` changes the run to `running` and writes activity `started`.
3. Before script execution, write activity `script_running`.
4. Before agent execution, write activity `agent_running`.
5. Parent-side runner polling periodically updates `heartbeat_at`.
6. If the parent receives or can infer meaningful agent progress, update `last_activity_at`, `last_activity_desc`, and `current_tool`.
7. Before delivery enqueue/dispatch, write activity `delivery_pending`.
8. After dispatch, write activity `delivery_dispatched`.
9. `complete_run()` writes terminal state and activity `completed`.

Late completion and lease-expired handling should preserve existing safety checks. If a run no longer owns its lease, activity updates should be ignored or return no-op.

## Runner Integration

The first phase should add an activity reporter boundary rather than deeply coupling the runner to SQLite.

Recommended shape:

- Scheduler passes `run_id` in the job dict as it already does.
- Runner resolves an optional activity callback or lightweight reporter from the job dict.
- Inprocess runner calls the reporter around script and agent phases.
- Subprocess parent loop writes heartbeat while waiting for child completion.
- Subprocess child may remain unaware in the first phase, but the protocol should allow child-to-parent activity messages later.

This preserves a clean path for future Hermes-style fine-grained activity:

- API call started/completed
- tool call started/completed
- stream delta received
- current tool name
- iteration count

Those future events should write through the same `update_run_activity` method and fields.

## CLI Status

`cron status` should remain readable. It should show summary information plus Top N details, with N defaulting to 5.

Keep existing lines:

- service status path
- scheduler service/process state
- service PID
- leader state
- last heartbeat
- last tick summary
- last service error
- exit reason
- cron home/sqlite/output/scripts
- runner mode
- job counts
- delivery queue stats

Add:

- `Next due:` earliest scheduled jobs, ordered by `next_run_at`
- `Running:` current running runs
- `Stale running:` running runs whose heartbeat or activity is stale
- `Last failed run:` most recent failed or abandoned run

Running job detail should include:

- job id
- job name
- run id
- started time
- heartbeat time
- last activity time
- last activity description
- current tool when set

Stale running should distinguish:

- heartbeat stale: process may be dead or blocked
- idle timeout exceeded: process alive but no useful progress
- lease expired: existing lease recovery should handle this, but status should make it visible until recovered

## Data Access Helpers

StateStore should expose focused query helpers instead of making CLI code write SQL directly.

Recommended helpers:

- `list_next_due_jobs(limit: int = 5) -> list[dict]`
- `list_running_runs(limit: int = 5) -> list[dict]`
- `list_stale_running_runs(now_text: str | None = None, limit: int = 5) -> list[dict]`
- `latest_failed_run() -> dict | None`
- `update_run_activity(...) -> dict | None`
- `resolve_job_timeouts(job: dict) -> dict`

The stale helper should consider job-specific idle timeout settings and heartbeat age. Heartbeat stale can use a conservative threshold derived from the job lease or a small multiple of scheduler poll interval.

## Error Handling

- Invalid job timeout values should be rejected at create/update boundaries when provided by users.
- Existing imported jobs should get null timeout columns and rely on environment defaults.
- Invalid `AGENT_CRON_TIMEOUT` should fall back to the current default behavior.
- Timeout completion should be idempotent. If a run is already terminal, later timeout handling must not overwrite it.
- Activity updates for missing or terminal runs should not crash scheduler ticks.
- If a status query fails, `cron status` should still print the existing service and path diagnostics, then include a concise warning.

## Testing Plan

Schema and persistence:

- new columns are added to existing SQLite databases
- old jobs import with null timeout fields
- create/update persists `idle_timeout_seconds` and `max_runtime_seconds`
- invalid timeout values are rejected

Timeout resolution:

- job idle timeout overrides `AGENT_CRON_TIMEOUT`
- `AGENT_CRON_TIMEOUT` provides default idle timeout
- zero or negative idle timeout disables idle timeout
- max runtime is disabled by default
- explicit max runtime is enforced

Run activity:

- claim writes `claimed`
- start writes `started`
- scheduler/runner phase updates are persisted
- heartbeat can update without changing `last_activity_at`
- terminal runs ignore late activity updates

Timeout behavior:

- idle timeout marks run failed with `exit_reason=idle_timeout`
- max runtime marks run failed with `exit_reason=max_runtime_exceeded`
- timeout error includes last activity and current tool details
- lease ownership checks still protect against late completion

CLI:

- `cron status` shows next due jobs
- `cron status` shows running jobs
- `cron status` shows stale running jobs
- `cron status` shows latest failed run
- existing service/leader/delivery status tests continue to pass

Runner modes:

- inprocess mode preserves existing behavior and records stage activity
- subprocess mode preserves existing timeout/termination behavior and records parent-side heartbeat

## Acceptance Criteria

- A running cron job has queryable heartbeat and activity fields in SQLite.
- Idle timeout uses activity time, not wall-clock runtime, by default.
- `AGENT_CRON_TIMEOUT` remains a compatible default idle timeout.
- Jobs may opt into `max_runtime_seconds`; otherwise long active jobs are not killed by a hard total runtime cap.
- Timeout failures are visible in run records through stable `exit_reason` values.
- `agent cron status` can show what is due next, what is currently running, what appears stale, and the latest failed run.
- Existing cron delivery, scheduler service, leader lease, and SQLite state machine behavior remain intact.
