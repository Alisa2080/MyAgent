# Cron Delivery Reliability Design

## Goal

Make cron delivery reliable and diagnosable for the first production-ready slice of the cron system. The immediate problem is that users can create scheduled jobs but cannot easily tell why a reminder or result did not appear.

This design focuses on four outcomes:

- `cron doctor` explains why cron delivery may not work.
- Delivery events survive process restarts.
- `cron test-delivery` verifies a target without running an agent job.
- `cron status` shows scheduler, job, and delivery queue health clearly.

The first delivery targets are `origin`, `local`, and `webhook`. Full project-style multi-platform adapters are intentionally out of scope for this phase.

## Current Context

Current cron delivery is split across:

- `cron/scheduler.py`: runs due jobs and calls `_deliver_result()`.
- `cron/notifications.py`: holds origin notifications in an in-memory queue.
- `agent_cli/cron_commands.py`: exposes `status` and `tick`.
- `cron/jobs.py`: stores jobs and last run metadata.

The current delivery path has three limits:

- Notifications are process-local and are lost on restart.
- `_deliver_result()` only supports `local` and `origin`.
- Status output does not explain pending, failed, or undeliverable results.

The `cron_reference/` reference files provide useful patterns for target parsing, delivery routing, richer CLI status, and diagnostics. They should be treated as reference material, not copied wholesale, because they depend onreference implementation-specific gateway and platform types.

## Non-Goals

- Do not implement Telegram, Slack, Discord, Matrix, or other platform adapters in this phase.
- Do not replace the cron scheduler architecture.
- Do not migrate job storage from `jobs.json` to SQLite.
- Do not add a standalone delivery worker thread yet.
- Do not change cron agent execution semantics beyond routing delivery results through the new delivery queue.

## Architecture

Add two focused modules:

### `cron/delivery_store.py`

Owns the SQLite database and delivery event state transitions. It does not send messages.

Responsibilities:

- Initialize and migrate the delivery schema.
- Enqueue delivery events.
- Claim due events for delivery.
- Mark events as delivered, failed, or dead.
- Reclaim stale `delivering` events.
- Return stats and recent errors for `status` and `doctor`.

### `cron/delivery.py`

Owns delivery target parsing, event construction, and actual delivery attempts.

Responsibilities:

- Parse `local`, `origin`, `webhook`, and `webhook:<url>`.
- Create delivery events from completed cron job runs.
- Process due delivery events.
- Deliver origin events through the existing notification drain API.
- Deliver webhook events with HTTP POST.
- Produce structured diagnostic results for `test-delivery`.

`cron/notifications.py` remains the compatibility surface for the REPL. Its public functions should continue to exist, but origin notifications should be backed by SQLite instead of only an in-memory queue.

## SQLite Storage

Database path:

```text
<cron_home>/cron/delivery.sqlite3
```

Primary table: `delivery_events`.

Columns:

- `id TEXT PRIMARY KEY`
- `job_id TEXT`
- `job_name TEXT`
- `run_at TEXT`
- `target TEXT`
- `target_type TEXT`
- `target_id TEXT`
- `status TEXT`
- `attempt_count INTEGER`
- `next_attempt_at TEXT`
- `last_attempt_at TEXT`
- `last_error TEXT`
- `output_path TEXT`
- `final_response TEXT`
- `payload_json TEXT`
- `created_at TEXT`
- `updated_at TEXT`

Valid statuses:

- `pending`: ready for a future delivery attempt.
- `delivering`: claimed by the current process.
- `delivered`: successfully delivered or confirmed local-only.
- `failed`: failed but still retryable.
- `dead`: no more retries or a non-retryable configuration error.

Indexes:

- `(status, next_attempt_at)` for claiming due deliveries.
- `(target_type, target_id, status, created_at)` for origin drains.
- `(job_id, created_at)` for job diagnostics.

## Delivery Targets

### `local`

`local` does not send a message. The cron output has already been saved by `save_job_output()`. The delivery system records a `delivered` event so `status` and `doctor` can show that the run had an intentional local-only delivery path.

### `origin`

`origin` targets the creating conversation thread. The target id is the stored origin `thread_id`.

If a job has `deliver=origin` but no `origin.thread_id`, the delivery is non-retryable and should become `dead` with a clear error.

If the thread is not currently being drained, the event remains `pending`; this is not a failure. `doctor` reports old pending origin events as a warning.

### `webhook`

Supported forms:

- `deliver="webhook:https://example.com/hook"`
- `deliver="webhook"` plus `AGENT_CRON_WEBHOOK_URL=https://example.com/hook`

Webhook delivery posts JSON and uses a short timeout.

Payload:

```json
{
  "type": "cron_result",
  "event_id": "...",
  "job_id": "...",
  "job_name": "...",
  "status": "ok",
  "run_at": "...",
  "final_response": "...",
  "error": null,
  "output_path": "..."
}
```

HTTP behavior:

- 2xx: mark `delivered`.
- 408, 429, 5xx, timeout, network error: mark `failed` and retry.
- Other 4xx: mark `dead`.
- Invalid or missing URL: mark `dead`.

## Retry Policy

Default maximum attempts: 5.

Retry delays:

```text
1m, 5m, 15m, 1h, 6h
```

When attempts exceed the maximum, the event becomes `dead`.

`delivering` events older than 10 minutes are stale. `doctor` should warn about them. `process_due()` may reclaim stale events by moving them back to `failed` with a fresh `next_attempt_at`.

## Scheduler Flow

The scheduler remains responsible for running jobs and saving output.

New delivery flow:

```text
_process_job()
  -> run_job()
  -> save_job_output()
  -> delivery.enqueue_result(job, result, output_path, run_at)
  -> delivery.process_due(limit=20)
  -> mark_job_run(... delivery_error=summary)
```

Rules:

- Delivery failure does not make the agent run fail.
- Delivery failure is recorded in `last_delivery_error`.
- Failed cron jobs should still enqueue a delivery event when target is `origin` or `webhook`.
- Successful results beginning with `[SILENT]` do not enqueue delivery events.
- Output is always saved before delivery decisions.

## CLI Commands

### `cron status`

Enhance the existing status output with delivery health:

```text
Scheduler: running
Cron home: ...
Jobs: 3 active / 4 total
Next run: 2026-05-28T...
Delivery queue: pending=2 failed=1 dead=0 delivered_recent=5
Last delivery error: job=abc target=webhook status=failed error=...
```

Exit code remains zero unless the command itself cannot read cron state.

### `cron doctor`

Add a diagnostic command that prints checks with `ok`, `warn`, or `fail`.

Checks:

- cron home exists and is writable.
- jobs file is readable and valid JSON.
- output directory is writable.
- scripts directory exists and is writable.
- delivery database can be opened and written.
- scheduler is running.
- active jobs have `next_run_at`.
- delivery targets are resolvable.
- pending origin deliveries are not excessively old.
- failed and dead deliveries are summarized.
- stale `delivering` events are reported.

Exit code:

- `0` when all checks are ok.
- `1` when any warning exists but no failures exist.
- `2` when any failure exists.

### `cron test-delivery`

Add a command that verifies a target without running an agent.

Examples:

```text
cron test-delivery --target local
cron test-delivery --target origin --session-id <thread_id>
cron test-delivery --target webhook:https://example.com/hook
```

Behavior:

- Creates a synthetic delivery event with `job_id="test-delivery"`.
- Uses the same enqueue and process path as real cron output.
- Prints event id, target, status, and last error.
- For `origin`, requires a session id unless run from an active session context.

Exit code:

- `0` when delivered or queued pending for origin.
- `1` when retryable failure occurs.
- `2` when target is invalid or event becomes dead.

## Compatibility

Keep these public functions available:

- `queue_cron_notification(thread_id, event)`
- `drain_cron_notifications_for_thread_id(thread_id, max_events=10)`
- `format_cron_notification_message(events, max_message_chars=6000)`
- `should_notify(final_response)`

`queue_cron_notification()` should enqueue an origin delivery event in SQLite. `drain_cron_notifications_for_thread_id()` should read pending origin events for the thread, return their event payloads, and mark them delivered after successful drain.

This keeps the REPL and existing tests close to their current integration points while replacing the volatile storage underneath.

## Error Handling

Delivery errors are separate from agent errors.

- Agent failure: `last_status=error`, `last_error` set, delivery event may still be created.
- Delivery failure: job run may remain successful, `last_delivery_error` set.
- Invalid target: delivery event becomes `dead`.
- Missing origin thread id: delivery event becomes `dead`.
- No active origin drain: event remains `pending`.
- Webhook timeout or 5xx: event becomes `failed` with next retry.
- Webhook 4xx except 408/429: event becomes `dead`.

## Testing

Add focused tests for:

- SQLite schema initialization.
- enqueue, claim, ack, fail, dead, and stats.
- stale `delivering` recovery.
- origin notification survives module reload or process-style reinitialization.
- `drain_cron_notifications_for_thread_id()` marks origin events delivered.
- webhook success, timeout, 4xx, and 5xx behavior.
- scheduler creates delivery events after job output is saved.
- `[SILENT]` suppresses delivery while preserving output.
- `cron status` includes delivery stats.
- `cron doctor` returns correct ok, warn, and fail exit codes.
- `cron test-delivery` uses delivery path and does not run an agent.

archived reference tests under `cron_reference/` should guide expected behavior, but test code should be written against this repository's module boundaries.

## Rollout

Implement in small steps:

1. Add `delivery_store.py` with tests.
2. Add `delivery.py` target parsing, enqueue, and local/origin behavior.
3. Adapt `notifications.py` to use SQLite for origin events.
4. Wire scheduler to enqueue and process delivery events.
5. Add webhook delivery.
6. Expand `status`, add `doctor`, add `test-delivery`.
7. Add regression tests for the full scheduler path.

The feature should be considered complete when a user can run `cron doctor`, identify why a reminder did not appear, run `cron test-delivery`, and see pending or failed delivery events persist across process restarts.
