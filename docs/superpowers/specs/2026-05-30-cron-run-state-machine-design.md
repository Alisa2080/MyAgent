# Cron Run State Machine Hardening Design

## Goal

Make cron execution facts durable and recoverable by formalizing the existing SQLite-backed `jobs`, `runs`, and `delivery_events` state machine.

This is the next phase after delivery architecture and scheduler service work. The repository already has `StateStore`, `runs`, delivery events with `run_id`, scheduler leader lease, and `agent cron serve`. This design does not introduce a new storage architecture. It tightens the current one so every due job has a persistent run record before execution, every delivery can be traced to that run, and scheduler crashes do not silently lose executions.

## Reference Direction

The Hermes cron reference is useful as a baseline for behavior, not as an implementation model to copy.

Hermes uses a gateway-driven tick, `jobs.json`, a file lock, early `advance_next_run()`, and `mark_job_run()` after execution. That gives simple operational behavior and user-friendly status fields, but it can lose an execution if the process advances `next_run_at` and then crashes before the job finishes.

The current project has already moved beyond that model by using SQLite, run records, claim leases, and a standalone cron service. The design should preserve the Hermes strengths that still apply:

- due-job behavior is easy to reason about,
- failed runs do not permanently wedge recurring jobs,
- job status remains understandable from CLI/status output,
- delivery errors are surfaced on the job and tied to saved output.

It should not preserve Hermes' early `next_run_at` advancement or JSON-file state mutation pattern.

## Scope

In scope:

- Define the authoritative state model for `jobs`, `runs`, and `delivery_events`.
- Make `StateStore.claim_due_jobs()` the only scheduler due-claim path.
- Ensure a run record is created before agent execution starts.
- Ensure `next_run_at` advances only when a run is completed, skipped, or explicitly recovered.
- Ensure delivery events created by cron job output carry `run_id`.
- Ensure delivery state changes sync back to `runs.delivery_status` and `jobs.last_delivery_error`.
- Recover expired `claimed` or `running` runs without losing the scheduled execution.
- Keep existing public cron APIs working where practical through compatibility wrappers.
- Add regression tests for crash/recovery, next-run advancement, delivery/run linkage, and legacy import behavior.

Out of scope for this phase:

- Full `cron logs JOB_ID` UI.
- `cron run --dry-run`.
- Explicit missed-run policies such as `skip`, `backfill-one`, `backfill-all`, or `max-backfill`.
- Job-level `idle_timeout` or `max_runtime`.
- Worker queue, backpressure, `concurrency_key`, or `replace_running`.
- Real new delivery adapters beyond the existing registry behavior.
- OS service installation or daemon supervision.

## Authoritative States

`job.state`:

- `scheduled`: eligible for future claims when enabled and due.
- `running`: one run currently owns the job lease.
- `paused`: user-disabled or intentionally not claimable.
- `completed`: no future runs remain.
- `error`: terminal error for non-recurring jobs or unrecoverable scheduling metadata.

`run.status`:

- `claimed`: scheduler has created the run and owns the job lease, but execution has not started.
- `running`: agent execution has started.
- `succeeded`: agent execution completed successfully.
- `failed`: agent execution completed with an error.
- `skipped`: scheduler intentionally skipped this scheduled occurrence.
- `abandoned`: scheduler lost or expired the lease before normal completion.

`delivery_events.status`:

- `pending`: queued for active dispatch or origin poll pickup.
- `delivering`: claimed by a dispatcher.
- `delivered`: completed successfully or synchronously audited for local delivery.
- `failed`: retryable failure.
- `dead`: non-retryable failure or unsupported persisted target.

## Core Invariants

- A due job cannot execute without a `runs` row.
- A `running` job must have `lease_run_id` and `lease_expires_at`.
- A run can only complete the job if it still owns the job lease.
- `next_run_at` is not advanced at claim time.
- `next_run_at` is advanced exactly once when the owning run completes or is intentionally skipped.
- A completed run clears `jobs.lease_run_id` and `jobs.lease_expires_at`.
- Delivery events created from job output include `job_id` and `run_id`.
- Poll-origin delivery events may have no `job_id` or `run_id`; those are not cron run deliveries.
- Delivery status aggregation for a run is deterministic from its delivery events.
- Expired `claimed` or `running` runs are marked `abandoned`; the job returns to `scheduled` unless it has no valid future schedule.

## Scheduler Flow

The scheduler tick uses this flow:

1. Recover expired run leases.
2. Claim due jobs transactionally with `StateStore.claim_due_jobs(now_text, limit)`.
3. For each claim, execute `_process_claimed(claimed, run_at, job_runner)`.
4. Mark the run `running` before invoking the job runner.
5. Save output after execution returns.
6. Enqueue delivery events with the run id attached.
7. Dispatch due active delivery events.
8. Sync delivery status for the run.
9. Complete the run and advance the job schedule.

The scheduler may keep the existing tick file lock as a local process guard, but the durable correctness boundary is the SQLite claim lease, not the file lock.

## Claim Semantics

`StateStore.claim_due_jobs()` owns due selection and claim mutation in one transaction.

Claim behavior:

- Select enabled `scheduled` jobs with `next_run_at <= now`.
- Apply the current grace/fast-forward behavior only as a transitional missed-run rule.
- Insert a `runs` row with `status='claimed'` and `scheduled_for=job.next_run_at`.
- Update the job to `state='running'`, set `lease_run_id`, and set `lease_expires_at`.
- Return a copy of the claimed job and run.

The old `cron.jobs.get_due_jobs()` and `cron.jobs.advance_next_run()` may remain for compatibility, but scheduler code must not use them for automatic execution.

## Completion Semantics

`StateStore.complete_run()` is the only normal completion path.

Completion behavior:

- Refuse late completion if the job no longer has `lease_run_id == run_id` or the lease expired.
- Mark late completions `abandoned` with `exit_reason='late_completion'`.
- For valid owners, set run status to `succeeded` or `failed`.
- Persist `output_path`, `final_response`, and `error`.
- Compute and persist the next job state:
  - `completed` when repeat count is exhausted or a one-shot is done,
  - `scheduled` for recurring jobs,
  - `error` only for unrecoverable one-shot failures.
- Increment repeat completion once for the accepted run.
- Clear job lease fields.
- Update `last_run_at`, `last_status`, `last_error`, and `last_delivery_error`.

The implementation should be idempotent enough that a duplicate completion attempt for the same run cannot double-increment repeat counts or advance the schedule twice.

## Delivery Linkage

Cron output delivery is part of the run audit trail.

Rules:

- `enqueue_result()` receives a job carrying `run_id` and persists that run id on every generated delivery event.
- Local delivery remains synchronous audit delivery: it creates a delivered event and does not require active dispatch.
- Origin delivery remains pending for origin poll or host bridge pickup.
- Webhook delivery remains active dispatch.
- After dispatch, `StateStore.update_run_delivery_status(run_id)` aggregates event statuses for the run.
- Retryable delivery failures update `runs.delivery_status` and `jobs.last_delivery_error`.
- Delivery failures do not retroactively change a successful agent run into a failed agent run; they are represented through delivery status and delivery error fields.

## Recovery Semantics

Crash recovery is lease-based.

`StateStore.recover_expired_leases(now_text)`:

- Finds jobs in `running` with expired `lease_expires_at`.
- Marks the owning `claimed` or `running` run `abandoned`.
- Sets `finished_at` and `exit_reason='lease_expired'`.
- Clears the job lease.
- Returns the job to `scheduled` so the same `next_run_at` can be claimed again, unless the schedule metadata is invalid.

This deliberately does not advance `next_run_at`. The next tick may retry the same scheduled occurrence, preserving the execution fact instead of silently skipping it.

## Compatibility

Existing APIs should keep working where practical:

- `cron.jobs.create_job()`, `list_jobs()`, `get_job()`, `update_job()`, and `remove_job()` continue to operate through SQLite-backed storage.
- Existing `jobs.json` import remains one-way and non-destructive.
- Legacy helper functions that no longer represent the scheduler path should be marked in code comments/docstrings as compatibility helpers.
- Tests should make it clear that automatic scheduling uses `StateStore.claim_due_jobs()`.

## Testing Goals

Add or tighten tests for:

- Claim creates a run before execution and does not advance `next_run_at`.
- Successful recurring completion advances `next_run_at` once.
- Failed recurring completion still advances according to current recurring semantics.
- One-shot completion disables or completes the job as expected.
- Duplicate or late completion cannot double-count repeat completion.
- Expired claimed/running lease marks run `abandoned` and returns job to `scheduled`.
- Delivery events created from cron output include `run_id`.
- Delivery failure updates run delivery status and job delivery error.
- Poll-origin delivery events can still exist without `run_id`.
- Imported `jobs.json` jobs enter the SQLite claim/complete path.
- Scheduler tick uses claim/complete flow and no longer calls automatic `advance_next_run()` before execution.

## Success Criteria

This phase is complete when:

- Automatic cron execution has a durable run row for every claimed occurrence.
- A crash after claim but before completion is recoverable and visible.
- `next_run_at` is not advanced before execution.
- Delivery events for cron output are traceable to runs.
- Run delivery status and job delivery errors stay in sync with delivery events.
- Existing delivery architecture and scheduler service tests still pass.
- The implementation leaves clear extension points for later missed-run policies, timeout tracking, concurrency queues, and logs UI.
