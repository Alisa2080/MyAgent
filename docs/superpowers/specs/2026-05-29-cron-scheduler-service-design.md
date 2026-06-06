# Cron Scheduler Service Design

Date: 2026-05-29

## Goal

Make `agent cron serve` the only recommended automatic scheduler entrypoint. The REPL must no longer start a background cron ticker. The service should be observable and safe to run in more than one process by recording heartbeat/status JSON and using an explicit leader lease before ticking jobs.

This phase does not redesign missed-run policy, worker queues, job concurrency semantics, system service installation, or timeout policy. It only moves automatic scheduling behind a service boundary and records enough state to diagnose whether that service is alive, leader, failing, or stopped.

## Current Context

The current scheduler can run due jobs through `cron.scheduler.tick()`. It already creates run records, uses job/run leases, enqueues delivery events, and serializes overlapping ticks with `.tick.lock`.

Automatic execution is still tied to `agent_core.cron_lifecycle.start_cron_scheduler()`, which starts a daemon thread from the REPL. That thread has no durable process identity, heartbeat, last tick timestamp, last service error, leader state, or exit reason.

Reference implementation keeps cron automatic execution attached to the gateway and uses a file lock to prevent overlapping ticks. This project should not copy the gateway dependency. Instead, it should take the useful parts of reference implementation's simple tick loop and explicit management commands, but expose cron as its own foreground service.

## User-Facing Behavior

`agent cron serve` starts a foreground cron scheduler service. It loops until interrupted, periodically writes status, competes for leader lease, and only calls `cron.scheduler.tick()` when it owns the lease.

`agent cron status` reads service status and leader lease state. It should no longer report automatic scheduling as running merely because the current REPL has a daemon thread.

`agent cron tick` remains a manual diagnostic command. It should still be safe around a running service. The existing tick lock remains a last line of defense; the preferred behavior is to make manual tick visibly respect service ownership, either by refusing when another live leader owns the scheduler lease or by reporting that the tick was skipped.

The REPL no longer starts or stops cron scheduler threads. Chat and CLI tools may continue to create, update, pause, resume, and trigger jobs, but automatic execution belongs to `agent cron serve`.

## Architecture

### `cron.service`

Add a small service loop module.

Responsibilities:

- Generate a stable per-process `owner_id`, such as `hostname:pid:uuid`.
- Handle SIGINT/SIGTERM for graceful shutdown.
- Write service heartbeat and status JSON.
- Try to acquire or renew leader lease before each tick.
- Call `cron.scheduler.tick()` only while leader.
- Record tick summary, service errors, and exit reason.

The core loop is:

1. Write `process_state=running` heartbeat.
2. Try to acquire or renew leader lease.
3. If follower, write `leader_state=follower` and sleep.
4. If leader, write `leader_state=leader`, `last_tick_started_at`, then call `scheduler.tick()`.
5. Write `last_tick_finished_at` and tick summary.
6. On loop exceptions, write `last_error` and keep serving unless shutdown is requested or startup configuration is invalid.
7. On shutdown, write `process_state=exited`, set `exit_reason`, and best-effort release the lease.

### `cron.leader`

Add explicit scheduler leader election.

The first implementation should use SQLite through the cron state database rather than a standalone JSON file, because the project already depends on SQLite for jobs, runs, and delivery events. Add a scheduler lease table, for example:

```sql
CREATE TABLE IF NOT EXISTS scheduler_leases (
  name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  pid INTEGER,
  hostname TEXT,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
)
```

`try_acquire_or_renew(name, owner_id, pid, hostname, now)` should atomically:

- Insert a lease if none exists.
- Renew if the current owner already owns it.
- Steal if `expires_at <= now`.
- Return follower state otherwise.

`release(name, owner_id)` deletes or clears only if the caller still owns the lease.

This lease is process-level ownership of scheduler ticking. It does not replace job/run leases in `StateStore`; those continue protecting individual claimed runs.

### `cron.service_state`

Add atomic status JSON read/write helpers.

Default path should live under cron home, for example `<cron_home>/status.json`. Writes should be atomic through a temporary file and replace operation.

Recommended JSON shape:

```json
{
  "version": 1,
  "service": "agent-cron",
  "owner_id": "host:12345:uuid",
  "pid": 12345,
  "hostname": "host",
  "process_state": "starting|running|stopping|exited|error",
  "leader_state": "leader|follower|lost|none",
  "lease_owner": "host:12345:uuid",
  "lease_expires_at": "2026-05-29T10:00:00+00:00",
  "started_at": "2026-05-29T09:59:00+00:00",
  "last_heartbeat_at": "2026-05-29T09:59:30+00:00",
  "last_tick_started_at": "2026-05-29T09:59:30+00:00",
  "last_tick_finished_at": "2026-05-29T09:59:31+00:00",
  "last_tick": {
    "due": 0,
    "ran": 0,
    "succeeded": 0,
    "failed": 0,
    "skipped": 0
  },
  "last_error": null,
  "exit_reason": null
}
```

`last_error` is for service loop or tick-level exceptions. Job execution failures remain in jobs/runs/delivery state and should not overwrite service health.

### CLI Integration

Add `agent cron serve` to the existing cron command parser. Options:

- `--interval SECONDS`, default 60.
- `--lease-seconds SECONDS`, default should be greater than interval, for example 180.
- `--once`, mainly for tests and diagnostics: attempt leadership, run at most one tick if leader, write status, exit.

Update `cron_status()` to include:

- Status JSON path.
- Process state and PID.
- Leader state.
- Lease owner and expiration.
- Last heartbeat.
- Last tick start/finish and summary.
- Last service error.
- Existing job counts, runner mode, delivery queue stats, and adapter keys.

Update `cron_doctor()` to warn when no fresh service heartbeat exists. A stale heartbeat means `last_heartbeat_at + lease_seconds` or a configured stale threshold is in the past.

### REPL Integration

Remove automatic REPL ticker startup. `agent_cli.repl.AgentCLI` should not call `start_cron_scheduler()` during construction or run lifecycle.

`agent_core.cron_lifecycle` can remain as deprecated compatibility code during this phase if removing it causes broad test churn, but no production REPL path should call it. Tests should assert this new behavior.

## Error Handling

Service startup errors should write `process_state=error` and `last_error` before exiting when possible.

Loop tick errors should be caught, recorded in status JSON, and followed by another sleep interval. A single bad tick should not kill the service.

Leader loss during a loop should set `leader_state=lost` or `follower` and skip further ticking until the lease can be reacquired. The existing job/run lease checks remain responsible for safe completion if lease timing changes during job execution.

Status JSON write failures should be logged and should not prevent the scheduler from ticking, but repeated failures will reduce observability. Tests should cover a normal write path; hardening retries can be a later improvement.

## Testing Plan

Add focused tests for:

- `agent cron serve --once` acquires leader lease, calls one tick, and writes status JSON.
- A second owner cannot acquire an unexpired leader lease.
- An expired lease can be acquired by another owner.
- Renew only succeeds for the current owner.
- Release only clears a lease for the owner that holds it.
- Service records `last_error` when `scheduler.tick()` raises and continues to write heartbeat state.
- Shutdown writes `process_state=exited` and an `exit_reason`.
- `cron status` renders missing, stale, follower, and active leader status clearly.
- REPL construction or startup no longer starts the cron lifecycle ticker.
- Manual `cron tick` is safe when the service owns the leader lease.

## Non-Goals

- No systemd, launchd, or Windows service installer.
- No missed-run policy redesign.
- No worker queue or backpressure implementation.
- No concurrency key behavior changes.
- No job-level idle timeout or max runtime schema changes.
- No origin delivery semantic changes.

## Acceptance Criteria

- `agent cron serve` is the documented automatic scheduler path.
- REPL no longer starts automatic cron ticking.
- Only the process holding scheduler leader lease calls `scheduler.tick()` from the service loop.
- Status JSON records heartbeat, process state, leader state, last tick, last error, and exit reason.
- `agent cron status` surfaces the new service and leader state.
- Existing scheduler tests continue to pass, and new service/leader/status tests cover the behavior above.
