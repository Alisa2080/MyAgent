# Cron Concurrency Policy and CLI Management Design

## Context

The cron system has already moved beyond the reference `cron_reference` design in the areas that matter for reliability: jobs, runs, and delivery events now live in SQLite; scheduler service status and leader leases exist; runs record activity and timeout exit reasons; delivery has a registry-backed dispatcher.

Reference implementation remains useful as a reference for operational simplicity and user-facing management commands, but its scheduler still relies on `jobs.json`, a tick file lock, pre-advancing `next_run_at`, and runner-side workdir serialization. The next step in this project should not copy that runner-level serialization. It should use the current SQLite state machine to make concurrency decisions at claim time and make those decisions observable through CLI commands.

## Goals

1. Add explicit per-job concurrency controls:
   - `concurrency_key`
   - `concurrency_policy`
2. Make the claim stage decide whether a due job is claimed, queued, skipped, or replaces active work.
3. Persist queued and skipped decisions as run records so cron history explains what happened after restarts.
4. Add CLI commands for inspecting runs, logs, deliveries, dry-run behavior, and delivery retries.
5. Keep the first stage focused: no process killing, no new global worker queue, and no complex log archival system.

## Data Model

Jobs gain:

- `concurrency_key`: optional string. Empty or missing values resolve to `job:{job_id}`.
- `concurrency_policy`: enum with values:
  - `queue_one`
  - `queue_all`
  - `replace_running`
  - `skip_if_running`

Defaults:

- `concurrency_key = job:{job_id}`
- `concurrency_policy = queue_one`

Runs gain:

- `concurrency_key`
- `concurrency_policy`
- `replaced_by_run_id`

`queue_position` should not be persisted in the first stage. CLI can compute it by ordering queued runs by `scheduled_for`, `created_at`, and `id`.

## Run States

The scheduler should treat these states as active:

- `queued`
- `claimed`
- `running`

Terminal states remain:

- `succeeded`
- `failed`
- `skipped`
- `abandoned`

`skipped` is used for `skip_if_running` decisions. It must be persisted as a run with `exit_reason=concurrency_skip`.

`abandoned` is used for stale runs and for `replace_running` state replacement. Replaced runs should use `exit_reason=replaced_by_newer_run` and set `replaced_by_run_id` when the new run id is known.

## Concurrency Policies

### `queue_one`

If the same `concurrency_key` has a `claimed` or `running` run, create one `queued` run.

If the same key already has a `queued` run, do not create another queued run. Advance the job's `next_run_at` so the same due instant is not reconsidered every tick.

This is the default policy because it avoids duplicate buildup while preserving one pending execution.

### `queue_all`

If the same key has an active run, create a new `queued` run for every due occurrence.

Queued runs are promoted in scheduled order. This is appropriate for jobs where every due occurrence matters.

### `replace_running`

When a new due run arrives, mark same-key active runs as `abandoned` with `exit_reason=replaced_by_newer_run`, then create a new `claimed` run.

The first stage must not try to kill the old process. If the old runner completes later, existing lease ownership checks should prevent delivery and should record late completion consistently.

### `skip_if_running`

If the same key has an active run, create a terminal `skipped` run with `exit_reason=concurrency_skip`, then advance `next_run_at`.

This keeps the history auditable: users can see that an execution was intentionally skipped because earlier work was still active.

## Tick Flow

Each scheduler tick should follow this order:

1. Recover stale work.
   - Complete idle-timed-out runs, heartbeat-stale runs, and lease-expired runs.
   - Recover stale delivery events.
   - This releases stale concurrency occupancy before new decisions are made.

2. Promote queued runs.
   - Scan queued runs ordered by `scheduled_for`, `created_at`, and `id`.
   - Promote a queued run to `claimed` only when its `concurrency_key` has no `claimed` or `running` run.
   - Promote at most one run per key per tick.

3. Claim due jobs.
   - Scan due jobs.
   - Resolve `concurrency_key` and `concurrency_policy`.
   - In a single SQLite `BEGIN IMMEDIATE` transaction, inspect active same-key runs and apply the policy.
   - Create `claimed`, `queued`, or `skipped` runs as appropriate.
   - Advance `next_run_at` as part of the claim decision.

4. Execute claimed runs.
   - `_run_parallel_claimed()` receives only runnable claimed runs.
   - Runner-level temporary serialization should not implement concurrency policy.
   - If the runner no longer owns the run lease before delivery, it must not enqueue delivery events.

5. Complete runs.
   - Success, failure, stale, replacement, and late-completion outcomes are recorded in runs.
   - Delivery state remains in delivery events and run `delivery_status`.

## Workdir Compatibility

Reference implementation serialized jobs with `workdir` because those jobs mutated process-global environment state. This project should not automatically turn workdir into the concurrency key unless implementation reveals the same process-global contamination still exists.

The first-stage default remains `job:{job_id}`. Users can explicitly set a shared key such as `workdir:/repo/path` or `repo:/repo/path` when they want multiple jobs to serialize against the same resource.

## CLI Management

CLI commands should follow the existing `agent_cli/cron_commands.py` `CronCommandResult` style. They should use SQLite store methods rather than runner internals.

### `agent cron runs [JOB_ID]`

Shows recent runs globally or for a specific job.

Default output should include:

- run id
- job id or job name
- status
- scheduled time
- started and finished time
- duration
- exit reason
- delivery status
- for active runs: last activity, current tool, and heartbeat time

The first stage should support `--limit`.

### `agent cron logs JOB_ID`

Shows job-centered diagnostics:

- job summary
- recent run summary
- latest failed run error
- latest output path
- final response or error preview

This command should not introduce a new log archival system. It should reuse `runs.output_path`, `runs.final_response`, and `runs.error`.

### `agent cron run JOB_ID --dry-run`

Does not create a real run, does not enqueue deliveries, and does not advance `next_run_at`.

It reports what would happen if the scheduler considered the job now:

- whether the job exists and is enabled
- whether it is currently due
- resolved concurrency key and policy
- decision: `would_claim`, `would_queue`, `would_skip`, `would_replace`, or `not_due`
- resolved timeout settings
- resolved delivery targets
- next scheduled time

Existing non-dry-run manual execution behavior should remain compatible in the first stage.

### `agent cron deliveries [JOB_ID|RUN_ID]`

Shows recent delivery events globally or filtered by job/run.

Output should include:

- event id
- job id
- run id
- target
- adapter key
- status
- attempt count
- next attempt time
- last error

When an ambiguous id could match both job and run, prefer an exact job match first and state the filter used in the output.

### `agent cron retry-delivery EVENT_ID`

Retries a failed or dead delivery event.

Rules:

- Missing event returns exit code 2.
- Events not in `failed` or `dead` return exit code 2.
- Successful retry reset sets `status=pending`, `next_attempt_at=now`, and `last_error=NULL`.
- `attempt_count` is not reset, preserving retry history and max-attempt semantics.
- The command does not force immediate dispatch in the first stage; the next scheduler or delivery dispatcher pass will process it.

### `agent cron status`

Enhance existing status output with:

- next due job
- running count and top active runs
- queued count and top queued runs
- stale count
- latest failed run or last cron error
- service last tick, last error, process state, leader state, lease owner, and lease expiry

Use summary plus top-N detail rather than dumping all rows.

## Agent Tool Schema

`agent_tools/public/cronjob.py` should accept `concurrency_key` and `concurrency_policy` for create and update actions. CLI is the primary target for this phase, but the tool schema must not lag permanently; otherwise chat-created cron jobs cannot configure the new scheduler behavior.

Validation should reject unknown policies on create/update. Existing stored jobs with missing or invalid policies should fall back to `queue_one` during read/claim, and should be visible in diagnostics.

## Error Handling

- Unknown policy during create/update: reject with a validation error.
- Unknown policy in old stored data: fallback to `queue_one`.
- Empty concurrency key: normalize to `job:{job_id}`.
- `replace_running` old-run completion after replacement: rely on lease checks to prevent delivery.
- `queue_one` duplicate queued case: advance `next_run_at` but do not add another run.
- Delivery retry storage failure: return exit code 1 with the database error.

## Testing Requirements

### State Store

- Default jobs get normalized concurrency fields.
- `queue_one` creates one queued run when same-key work is active.
- `queue_one` does not create duplicate queued runs for the same key.
- `queue_all` creates a queued run for each due occurrence.
- `skip_if_running` creates a skipped run with `exit_reason=concurrency_skip`.
- `replace_running` abandons active same-key runs and creates a new claimed run.
- Queued promotion promotes only one run per key per tick.
- Queued promotion can promote different keys in the same tick.
- Queued runs survive restart and promote after active same-key work completes.
- Late completion from a replaced run does not enqueue delivery.

### Scheduler

- Tick ordering is stale recovery, queued promotion, due claim, execution.
- `_run_parallel_claimed()` no longer makes concurrency-policy decisions.
- Delivery enqueue happens only while the run still owns its lease.
- Existing activity and timeout behavior still works for claimed/running runs.

### CLI

- `runs` works globally and filtered by job id.
- `logs JOB_ID` shows recent runs, latest error, and output path.
- `run JOB_ID --dry-run` creates no run, advances no schedule, and enqueues no delivery.
- `deliveries` works globally and filtered by job or run.
- `retry-delivery` resets failed/dead events and rejects pending/delivered events.
- `status` shows running, queued, stale, next due, last tick, and last error summaries.

## Non-Goals

- Do not terminate old runner processes for `replace_running`.
- Do not add a global worker queue or backpressure system.
- Do not add complex log file archival or tailing.
- Do not build a TUI or web management UI.
- Do not change the delivery adapter architecture beyond the CLI inspection and retry surfaces.

## Open Decisions

No open decisions remain for the first-stage design. The approved choices are:

- `skip_if_running` records a skipped run.
- `queue_one` and `queue_all` persist queued runs in the runs table.
- `replace_running` performs state replacement only and does not kill processes.
- Defaults are `concurrency_key=job:{job_id}` and `concurrency_policy=queue_one`.
