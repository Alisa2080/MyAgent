# Cron SQLite Delivery State Machine Design

## Goal

Move cron reliability from JSON files plus hardcoded delivery branches to a single SQLite-backed state machine for jobs, runs, and deliveries.

This design intentionally combines two changes:

- Delivery becomes adapter-driven, persistent, retryable, and multi-target.
- Job execution becomes run-based with claim leases instead of pre-advancing `next_run_at` before execution.

The user-visible result is that cron outputs have an auditable path from due job to run record to delivery event, and failures can be diagnosed without guessing which process was alive.

## Current Context

The current implementation has made progress but still has structural limits:

- `cron/delivery_store.py` persists delivery events, but delivery dispatch is still hardcoded around `local`, `webhook`, and `unsupported`.
- `cron/jobs.py` stores jobs in `jobs.json`, so job updates and due-job selection are not transactional.
- `cron/scheduler.py` calls `advance_next_run()` before running the job, which can lose a scheduled execution if the process crashes after advancing and before completing the run.
- `origin` means `origin.thread_id`, which works for a single CLI thread but does not describe gateway chat, web session, or future platform delivery.

The `hermes_agent_cron/` reference implementation provides useful patterns:

- Target parsing for `origin`, `local`, platform names, and `platform:chat_id[:thread_id]`.
- Origin capture with platform, chat, and thread metadata.
- Topic/thread preservation warnings.
- Live gateway adapter first, standalone fallback later.
- Long-output truncation with full output saved locally.

Those patterns should inform the design, but the current repository should get a smaller native implementation first.

## Scope

In scope:

- Add a SQLite cron state store for jobs, runs, and delivery events.
- Automatically import existing `jobs.json` once, then preserve it read-only.
- Add `DeliveryAdapter`, `DeliveryRegistry`, and `DeliveryDispatcher`.
- Migrate existing `local`, `origin`, and `webhook` behavior into adapters.
- Support comma-separated multi-target delivery.
- Replace execution-time pre-advance with claim/lease/complete transitions.
- Keep existing public function names and CLI/tool behavior where practical.

Out of scope for this implementation:

- Real Slack, Discord, email, Telegram, Matrix, or gateway live adapter sends.
- A standalone long-running `agent cron serve` process.
- Full missed-run policy configuration beyond the current grace/skip behavior.
- Replacing the current workdir/non-workdir concurrency policy.

## SQLite Store

Add `cron/state_store.py`.

Default database path:

```text
<cron_home>/cron.sqlite3
```

The state store owns schema initialization, migrations, and all transactional state changes. Existing modules call the store through small facades so the public API remains stable.

### Tables

`schema_meta`

- `key TEXT PRIMARY KEY`
- `value TEXT NOT NULL`
- Used for schema version and `jobs_json_imported_at`.

`jobs`

- `id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `prompt TEXT NOT NULL`
- `schedule_json TEXT NOT NULL`
- `schedule_display TEXT`
- `enabled INTEGER NOT NULL`
- `state TEXT NOT NULL`
- `next_run_at TEXT`
- `last_run_at TEXT`
- `last_status TEXT`
- `last_error TEXT`
- `last_delivery_error TEXT`
- `repeat_json TEXT NOT NULL`
- `deliver TEXT NOT NULL`
- `delivery_targets_json TEXT`
- `origin_json TEXT`
- `workdir TEXT`
- `script TEXT`
- `context_from_json TEXT`
- `skills_json TEXT`
- `enabled_toolsets_json TEXT`
- `model TEXT`
- `provider TEXT`
- `base_url TEXT`
- `concurrency_key TEXT`
- `lease_run_id TEXT`
- `lease_expires_at TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Valid job states:

- `scheduled`
- `running`
- `paused`
- `completed`
- `error`

`runs`

- `id TEXT PRIMARY KEY`
- `job_id TEXT NOT NULL`
- `scheduled_for TEXT NOT NULL`
- `claimed_at TEXT NOT NULL`
- `lease_expires_at TEXT`
- `started_at TEXT`
- `finished_at TEXT`
- `attempt INTEGER NOT NULL`
- `status TEXT NOT NULL`
- `exit_reason TEXT`
- `output_path TEXT`
- `final_response TEXT`
- `error TEXT`
- `delivery_status TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Valid run statuses:

- `claimed`
- `running`
- `succeeded`
- `failed`
- `skipped`
- `abandoned`

`delivery_events`

- `id TEXT PRIMARY KEY`
- `job_id TEXT`
- `run_id TEXT`
- `job_name TEXT`
- `run_at TEXT`
- `target TEXT NOT NULL`
- `target_type TEXT NOT NULL`
- `adapter_key TEXT NOT NULL`
- `address TEXT`
- `thread_id TEXT`
- `origin_json TEXT`
- `status TEXT NOT NULL`
- `attempt_count INTEGER NOT NULL`
- `next_attempt_at TEXT`
- `last_attempt_at TEXT`
- `last_error TEXT`
- `output_path TEXT`
- `final_response TEXT`
- `payload_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Valid delivery statuses:

- `pending`
- `delivering`
- `delivered`
- `failed`
- `dead`

Indexes:

- `jobs(state, enabled, next_run_at)`
- `jobs(lease_expires_at)`
- `runs(job_id, scheduled_for)`
- `runs(status, lease_expires_at)`
- `delivery_events(status, next_attempt_at)`
- `delivery_events(job_id, run_id, created_at)`
- `delivery_events(target_type, address, status, created_at)`

## Jobs JSON Migration

Existing `jobs.json` is automatically imported once.

Rules:

- On SQLite initialization, if no jobs exist and `jobs.json` exists, import it in one transaction.
- Preserve each old job id.
- Convert old `origin` into `origin_json`.
- Parse old `deliver` into `delivery_targets_json` where possible.
- Record `jobs_json_imported_at` in `schema_meta` after successful import.
- Leave `jobs.json` on disk unchanged.
- After import, SQLite is the only write target.
- If import fails, initialization fails with a clear error. Do not silently create an empty database.

This is fail-safe for existing users: old jobs are retained, and the old file remains available for manual inspection or rollback.

## Delivery Identity

Replace the old `origin.thread_id`-only model with structured origin identity.

`DeliveryIdentity` fields:

- `source_type`: `cli`, `gateway`, `web`, `api`, or `unknown`.
- `platform`: gateway platform such as `slack`, `discord`, `telegram`, or `email`; CLI may use `cli` or null.
- `chat_id`: gateway channel, room, thread, user, or chat id.
- `thread_id`: gateway topic/thread id or LangGraph thread id when applicable.
- `session_id`: CLI or web session id.
- `display_name`: optional human-readable label.

`deliver=origin` is fail closed:

- Job creation rejects origin delivery when no origin identity can be captured.
- Delivery marks the event `dead` if an existing event has incomplete origin identity.
- There is no fallback to local or home channel.

## Delivery Targets

`DeliveryTarget` fields:

- `raw`: original target string.
- `target_type`: `local`, `origin`, `webhook`, or `platform`.
- `adapter_key`: registry key, such as `local`, `origin`, `webhook`, or a future platform key.
- `address`: webhook URL, chat id, or other destination reference.
- `thread_id`: platform topic/thread when present.
- `metadata`: JSON-compatible extension fields.

Supported target syntax in this phase:

- `local`
- `origin`
- `webhook`
- `webhook:<url>`
- comma-separated combinations such as `origin,webhook:https://example.invalid/hook,local`

Reserved syntax:

- `platform`
- `platform:chat_id`
- `platform:chat_id:thread_id`

Reserved platform targets parse into `DeliveryTarget`, but real sending requires a registered platform adapter. Without one, creation/update should reject the target by default. Internal migration may preserve unsupported legacy targets and let dispatcher mark them `dead` with a clear error.

Multi-target rules:

- Each target becomes one `delivery_events` row.
- Deduplicate by `(target_type, adapter_key, address, thread_id)`.
- One target failure does not block other targets.
- Run delivery status is aggregated after dispatch.

## Delivery Components

### `cron/delivery_targets.py`

Owns target parsing and normalization.

Responsibilities:

- Parse comma-separated delivery strings.
- Preserve webhook URLs without corrupting `https://`.
- Resolve `origin` into a target only when origin identity exists.
- Deduplicate targets.
- Return validation errors suitable for CLI/tool responses.

### `cron/delivery_adapters.py`

Defines the adapter protocol and first built-in adapters.

Protocol:

```python
class DeliveryAdapter(Protocol):
    key: str

    def validate(self, target: DeliveryTarget, job: dict[str, Any]) -> DeliveryValidation:
        ...

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        ...
```

`DeliveryResult` distinguishes:

- delivered
- retryable failure
- permanent failure

Built-in adapters:

- `LocalDeliveryAdapter`: confirms the output path exists and marks delivered.
- `OriginDeliveryAdapter`: supports CLI/thread drain delivery now; gateway/web origin fails closed until a live adapter is registered.
- `WebhookDeliveryAdapter`: preserves current webhook POST behavior and URL safety checks.

### `cron/delivery_registry.py`

Owns adapter registration and lookup.

Responsibilities:

- Register built-in adapters.
- Validate delivery targets at create/update time.
- Resolve event adapter by `adapter_key`.
- Provide diagnostics for `cron doctor`.

### `cron/delivery_dispatcher.py`

Owns persistent event dispatch.

Flow:

```text
dispatch_due(limit)
  -> state_store.claim_due_delivery_events()
  -> registry.get(event.adapter_key)
  -> adapter.deliver(event, job, run)
  -> mark delivered, failed, or dead
  -> update run delivery aggregate
```

Retry policy:

- Maximum attempts: 5.
- Backoff: 1m, 5m, 15m, 1h, 6h.
- Retryable: timeout, network error, 408, 429, 5xx.
- Permanent: invalid target, missing origin, unsupported adapter, non-retryable 4xx.

## Scheduler State Machine

Keep `cron.scheduler.tick()` as the public entry point.

New tick flow:

```text
tick(now)
  -> recover_expired_leases(now)
  -> claim_due_jobs(now, limit)
  -> execute each claimed run
  -> save output
  -> enqueue run deliveries
  -> complete run and update job next_run_at/state/repeat
  -> dispatch due delivery events
```

Claim rules:

- `claim_due_jobs()` runs in a transaction.
- It selects enabled `scheduled` jobs with `next_run_at <= now`.
- It creates a `runs` row with status `claimed`.
- It sets job state to `running`, `lease_run_id`, and `lease_expires_at`.
- It does not advance `next_run_at`.

Completion rules:

- On success, run becomes `succeeded`.
- On agent failure, run becomes `failed`.
- Output is saved before delivery events are enqueued.
- `complete_run()` computes and writes the next `next_run_at`.
- One-shot completed jobs become `completed` and disabled.
- Recurring jobs return to `scheduled` unless paused.

Lease recovery:

- A run whose lease expires before completion becomes `abandoned`.
- The job is returned to `scheduled` or `error` depending on schedule health.
- The first implementation uses a conservative default lease duration, configurable by environment or store option.

Missed run policy:

- Preserve current behavior for the first SQLite phase.
- Recurring jobs outside their grace window are skipped and advanced to the next future run.
- Record a `skipped` run for observability.
- Do not backfill all missed runs yet.

Concurrency:

- Preserve current execution policy: jobs with `workdir` run serially; jobs without `workdir` may run in parallel.
- Store `concurrency_key` now, defaulting to `workdir` when present and `job_id` otherwise.
- Do not implement `queue_one`, `queue_all`, or `replace_running` yet.

## Compatibility Facades

Keep existing imports working:

- `cron.jobs.create_job`
- `cron.jobs.update_job`
- `cron.jobs.list_jobs`
- `cron.jobs.get_job`
- `cron.jobs.pause_job`
- `cron.jobs.resume_job`
- `cron.jobs.trigger_job`
- `cron.jobs.save_job_output`
- `cron.delivery.enqueue_result`
- `cron.delivery.process_due`
- `cron.delivery_store.DeliveryStore`

These functions/classes become wrappers around `StateStore`, `DeliveryRegistry`, and `DeliveryDispatcher` where needed.

The public shape of listed jobs should remain compatible with existing CLI and tests:

- `job_id` or `id`
- `name`
- `schedule`
- `schedule_display`
- `next_run_at`
- `last_run_at`
- `last_status`
- `last_error`
- `last_delivery_error`
- `state`
- `enabled`

## CLI and Tool Behavior

`cron create` and the `cronjob` tool:

- Validate target strings through `DeliveryRegistry`.
- `deliver=origin` requires captured origin identity.
- `deliver` omitted in an active CLI session may still default to origin if identity exists.
- Top-level CLI create without a session defaults to local unless the user explicitly chooses another valid target.

`cron status`:

- Show SQLite path.
- Show job counts by state.
- Show running runs and stale leases.
- Show delivery stats.
- Show the most recent delivery error.

`cron doctor`:

- Check SQLite readability/writability.
- Check jobs JSON import marker if old `jobs.json` exists.
- Check delivery adapter registry.
- Check unsupported delivery targets.
- Check incomplete origin identities.
- Check stale running runs and stale delivering events.

`cron test-delivery`:

- Use the registry and dispatcher.
- Create test events without running an agent.
- For origin, require an explicit or current session identity.

## Error Handling

Delivery failure does not make the agent run fail.

Run status and delivery status are separate:

- A run can be `succeeded` while delivery is `failed` or `partial`.
- A failed run can still have delivery events so the user sees the failure.
- `[SILENT]` successful responses suppress delivery events but still save output and mark run success.

Permanent validation errors should be caught early at create/update time when possible. Events created by migration or legacy paths can still become `dead` during dispatch.

## Testing

Add focused tests for:

- SQLite schema initialization.
- Automatic `jobs.json` import and read-only preservation.
- Job create/list/update through existing `cron.jobs` facade.
- `deliver=origin` fail-closed creation without identity.
- Structured origin identity storage for CLI sessions.
- Multi-target delivery creates one event per target.
- Local adapter marks delivered.
- Webhook adapter preserves current retry/dead behavior.
- Unsupported platform target is rejected at create/update.
- `claim_due_jobs()` creates run records without pre-advancing `next_run_at`.
- `complete_run()` advances next run after execution.
- Lease recovery marks stale runs abandoned.
- Skipped missed recurring run records are created.
- Existing CLI command tests continue to pass with updated sqlite status text.

## Rollout Plan

Implement behind the existing API names. The migration occurs automatically when state is first opened, so no user command is required.

Recommended implementation order:

1. Add `StateStore` schema and jobs JSON import.
2. Move `cron.jobs` facade to SQLite.
3. Add run claim/complete/lease recovery.
4. Add target parsing, adapters, registry, and dispatcher.
5. Wire scheduler to claim runs and enqueue deliveries.
6. Update CLI/tool diagnostics.
7. Expand tests and remove direct dependencies on JSON writes.

## Risks

- The automatic migration touches all cron paths at once. Mitigate with a transactional import and keeping `jobs.json` unchanged.
- Existing tests may assume JSON file writes. Mitigate by keeping facade output stable and updating tests to inspect behavior instead of storage internals.
- Origin identity can be incomplete for legacy jobs. Mitigate by fail-closing with explicit errors instead of guessing.
- Platform syntax may look supported before real platform adapters exist. Mitigate by rejecting unsupported adapters at create/update time.
