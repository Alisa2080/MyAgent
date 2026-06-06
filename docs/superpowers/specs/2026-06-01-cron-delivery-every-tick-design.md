# Cron Delivery Every Tick Design

## Context

The cron scheduler already has a durable delivery queue. Cron job outputs are
enqueued as `delivery_events`, `DeliveryDispatcher.dispatch_due()` claims due
non-origin events, and retryable failures move through `failed` with backoff
until they become `dead`. Origin events are intentionally left for the host
thread to drain through `cron.notifications.drain_cron_notifications_for_thread_id`.

The current gap is scheduling. Delivery dispatch is mainly triggered when a job
finishes and `_process_claimed()` calls `process_due()`. If no cron job is due,
pending or failed delivery events may not advance, and stale `delivering` events
may not be recovered promptly. reference implementation dispatches inline while processing a due
job; this project has a stronger SQLite delivery queue, so the next step should
make the queue progress independent of job execution.

This phase keeps origin polling out of the cron service. The scheduler will
maintain the outbound delivery queue every tick, while host applications remain
responsible for origin notification drain and any inbound pollers.

## Goals

1. Run delivery maintenance during every scheduler tick, even when there are no
   due jobs.
2. Recover stale `delivering` delivery events at the start of tick processing.
3. Dispatch due `pending` and `failed` non-origin delivery events on every tick.
4. Keep origin events pending for host-side drain; do not let the dispatcher
   claim them.
5. Make delivery maintenance visible in `TickResult`, cron service status, and
   `agent cron tick` output.
6. Document the host integration contract for:
   - `drain_cron_notifications_for_thread_id(thread_id)`
   - `origin_poller.poll_deliveries(pollers, store=None, limit=100)`

## Non-Goals

- Do not implement automatic origin poller discovery.
- Do not register platform inbound pollers with the cron service.
- Do not change the origin notification drain API.
- Do not redesign delivery adapters or delivery target parsing.
- Do not treat pending origin events as delivery failures.
- Do not change job completion semantics beyond preserving existing fast
  dispatch after a job enqueues delivery events.

## Tick Flow

The scheduler tick should keep the current lock and run-state ordering, with one
new delivery maintenance phase before queued/due job processing:

1. Acquire `_TickLock`.
2. Create `StateStore`.
3. Recover expired run leases.
4. Complete stale running runs where appropriate.
5. Run delivery maintenance.
6. Promote queued runs.
7. Claim due jobs.
8. Execute claimed runs.
9. Complete runs and enqueue job output deliveries.
10. Dispatch newly enqueued non-origin deliveries so completed jobs still deliver
    quickly in the same tick.

The important behavior change is step 5: a tick with no due jobs still advances
delivery retry and stale-delivery recovery.

## Delivery Maintenance

Add a small scheduler-level helper. The exact function name can change during
implementation, but the boundary should remain in `cron.scheduler` for this
phase:

```python
def _process_delivery_maintenance(
    store: StateStore,
    *,
    limit: int = 20,
) -> DeliveryTickSummary:
    raise NotImplementedError
```

Responsibilities:

- recover stale delivery events before new claims
- dispatch due non-origin events through the existing delivery registry and
  dispatcher
- return a structured summary
- catch maintenance-level exceptions so job scheduling can continue

`DeliveryDispatcher.dispatch_due()` currently calls
`recover_stale_delivery_events()` internally. To make tick ordering explicit and
avoid double recovery, change the dispatcher boundary to accept a flag:

```python
def dispatch_due(
    self,
    *,
    limit: int = 20,
    adapter_keys: set[str] | None = None,
    recover_stale: bool = True,
) -> dict[str, int]:
    raise NotImplementedError
```

Compatibility rule:

- Existing `process_due()` keeps default behavior and still recovers stale
  events unless a caller explicitly disables it.
- Scheduler delivery maintenance calls the dispatcher with
  `recover_stale=False` after it performs explicit recovery.

## Tick Result Model

Extend `TickResult` with a delivery summary while preserving existing fields:

```python
@dataclass
class DeliveryTickSummary:
    recovered_stale: int = 0
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    error: str | None = None


@dataclass
class TickResult:
    due: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    delivery: DeliveryTickSummary = field(default_factory=DeliveryTickSummary)
    results: list[JobTickResult] = field(default_factory=list)
```

`CronService._tick_summary()` should include delivery fields:

```python
"delivery": {
    "recovered_stale": result.delivery.recovered_stale,
    "claimed": result.delivery.claimed,
    "delivered": result.delivery.delivered,
    "failed": result.delivery.failed,
    "dead": result.delivery.dead,
    "error": result.delivery.error,
}
```

`agent cron tick` should include a delivery line:

```text
Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0
```

If maintenance failed:

```text
Delivery tick: error=Runtime error during delivery maintenance
```

## Error Handling

Delivery maintenance should not abort the whole scheduler tick.

- If stale recovery fails, record the error in `DeliveryTickSummary.error` and
  continue to queued/due job processing.
- If dispatch fails at the maintenance level, record the error and continue to
  queued/due job processing.
- Per-event delivery failures continue using existing dispatcher semantics:
  retryable events become `failed` with backoff; terminal failures become
  `dead`.
- Origin pending events stay pending and are not counted as failed.
- Unsupported non-origin targets continue to be dead-lettered by the dispatcher.

If a job completes and delivery enqueue/dispatch fails in the existing
job-completion path, this phase does not redesign that behavior.

## Origin Host Contract

Origin delivery is a host responsibility in this phase.

### Notification Drain

Embedding applications that support same-thread cron notifications must call:

```python
from cron.notifications import drain_cron_notifications_for_thread_id

events = drain_cron_notifications_for_thread_id(thread_id, max_events=10)
```

Contract:

- `thread_id` is the host conversation/session identifier used when the cron job
  was created with `deliver="origin"`.
- Returned events are payload dictionaries ready for host-side presentation or
  agent resumption.
- Persisted origin events are marked delivered after successful drain.
- If the host never drains, events remain pending and are visible as origin
  backlog, but they are not delivery dispatch failures.
- CLI REPL already drains at idle points; non-CLI hosts must wire this call into
  their own message loop or resume workflow.

### Origin Poller

`cron.origin_poller.poll_deliveries(pollers, store=None, limit=100)` remains a
host utility for inbound platform polling.

Contract:

- The cron service does not auto-discover or run pollers in this phase.
- Hosts that own platform adapters may call `poll_deliveries()` on their own
  cadence.
- Poller cursor state is persisted by adapter key.
- Polled events are enqueued into the delivery store for host-specific handling,
  but automatic platform inbound processing is outside this phase.

## Observability

`agent cron status` already shows delivery queue counts and origin pending
counts. This phase should add or preserve enough service status to answer:

- Did the last tick run delivery maintenance?
- Did it recover stale delivery events?
- Did it dispatch due retry events?
- Did delivery maintenance fail?
- Are origin events pending for host drain?

The status output should stay compact. It does not need to list every delivery
event; `agent cron deliveries` remains the detailed inspection command.

## Testing Requirements

### Scheduler

- A tick with no due jobs processes a due failed webhook delivery.
- A tick with no due jobs recovers stale `delivering` events before dispatch.
- Pending origin events are not claimed by delivery maintenance.
- A due job that enqueues a webhook delivery still dispatches the newly enqueued
  event in the same tick.
- Delivery maintenance failure does not prevent due jobs from running.

### Dispatcher and Store

- `DeliveryDispatcher.dispatch_due(recover_stale=False)` does not recover stale
  events itself.
- Existing `process_due()` keeps recovering stale events by default.
- Dispatcher summary remains backward-compatible with existing keys:
  `claimed`, `delivered`, `failed`, and `dead`.

### Service and CLI

- `_tick_summary()` includes the nested delivery summary.
- `agent cron tick` renders the delivery summary line.
- `agent cron status` continues to show delivery queue and origin pending counts.

### Host Contract

- Existing notification tests should continue proving that
  `drain_cron_notifications_for_thread_id()` drains persisted origin events and
  marks them delivered.
- Documentation should state that origin pollers are host-called and not
  scheduler-discovered in this phase.

## Rollout Notes

This is a behavior improvement for existing cron services. Users do not need new
configuration to get delivery retry progress; once the cron service is ticking,
delivery maintenance runs even when no jobs are due.

Hosts that rely on origin delivery must still drain notifications. This design
intentionally avoids hiding host integration requirements behind automatic
polling or discovery.
