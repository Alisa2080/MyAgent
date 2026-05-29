# Cron Delivery Boundary Design

## Goal

Tighten the cron delivery boundary so target parsing and validation are owned by
`DeliveryRegistry.validate_targets()`, while `enqueue_result()` only converts
validated targets into durable delivery events.

The concrete acceptance case is platform extensibility: after registering a fake
`slack` adapter, `deliver="slack:C123"` should create, enqueue, dispatch, and
mark delivered without adding a Slack-specific branch to `cron/delivery.py`.
Without that adapter, the same target should fail through the registry with an
unsupported delivery target error.

## Current State

The project already has the right major pieces:

- `DeliveryAdapter` and `DeliveryRegistry` define adapter validation and
  dispatch interfaces.
- `DeliveryDispatcher` claims persisted `delivery_events`, looks up adapters by
  `adapter_key`, and calls `adapter.deliver(event, job, run)`.
- `DeliveryIdentity` represents origin as structured identity data rather than
  only `origin.thread_id`.
- `parse_delivery_targets()` supports comma-separated targets, dedupes them, and
  can parse `platform:chat_id[:thread_id]`.

The remaining leak is in `enqueue_result()`. It still knows specific target
rules, including webhook URL validation and unsupported platform checks. That
means a new adapter can be parseable but still require edits to core enqueue
logic before it works end to end.

Hermes is useful as a semantic reference for resolving comma-separated targets
and preserving platform/chat/thread identity. It should not be copied as the
implementation shape, because Hermes keeps delivery behavior in a monolithic
`_deliver_result()` path. The current project already has a stronger
registry/dispatcher model.

## Design

`DeliveryRegistry.validate_targets()` becomes the single validation boundary.
It parses the `deliver` string, checks that every target has a registered
adapter, and asks each adapter to validate its target. The resulting
`DeliveryValidation.targets` list is the only source `enqueue_result()` uses to
create delivery events.

`enqueue_result()` will:

1. Build the payload once.
2. Build `DeliveryIdentity` from `job["origin"]`.
3. Call `registry.validate_targets(job.get("deliver"), origin=origin, job=job)`.
4. If validation fails, enqueue one `dead` delivery event with the validation
   error so the failure is visible in run/status/log flows.
5. If validation succeeds, enqueue one event per validated target using the
   target's `raw`, `target_type`, `adapter_key`, `address`, `thread_id`, and
   origin metadata.

It will not contain target-specific branches such as:

- `target.target_type == "webhook"`
- `target.target_type == "platform"`
- direct calls to `validate_webhook_url()`
- direct checks for missing platform adapters

The dispatcher remains adapter-key based. It should not learn Slack, Discord,
Email, or webhook semantics. Given an event with `adapter_key="slack"` and a
registry containing a `slack` adapter, it claims the event and calls that
adapter. Given no adapter, registry validation prevents new jobs from creating
or enqueueing valid pending events for that target.

## Local And Origin Semantics

This design does not attempt to fully redesign `local` or `origin`.

For this step, `local` can keep the existing behavior if tests require it:
successful local deliveries may be recorded as already delivered at enqueue
time. The important boundary is that the target was validated by the registry,
not by `enqueue_result()` manually.

`origin` can also keep the current poller-oriented behavior. It must still be
validated through `OriginDeliveryAdapter.validate()`, and the persisted event
must keep structured origin metadata. A later design can decide whether origin
should become a live adapter, a poll-only adapter, or a distinct delivery mode.

## Adapter Extensibility

A new delivery adapter should need only:

- an adapter object with `key`, `validate(target, job)`, and
  `deliver(event, job, run)`;
- registration in the registry used by create/update/enqueue/dispatch;
- tests proving its target string creates the expected durable event and
  dispatches through the adapter.

The first proof should use a fake `slack` adapter in tests rather than a real
Slack implementation. This proves the boundary without adding provider-specific
runtime dependencies.

## Error Handling

Validation failures should be durable. If a previously valid job becomes invalid
because an adapter is unavailable or its stored target is malformed,
`enqueue_result()` should create a `dead` delivery event carrying the registry
error. This keeps the failure observable without retrying a configuration error.

Adapter delivery failures remain dispatcher concerns:

- `DeliveryResult(delivered=True)` marks the event delivered.
- retryable failures go through `mark_delivery_failed()`.
- non-retryable failures become dead events.
- adapter exceptions are caught by the dispatcher and counted as failed or dead
  according to retry exhaustion.

## Testing

Focused tests should cover:

- webhook validation still comes from `WebhookDeliveryAdapter.validate()`;
- unsupported `slack:C123` fails through `DeliveryRegistry.validate_targets()`;
- registered fake `slack` validates `slack:C123`, creates a job, persists
  `delivery_targets_json` with `adapter_key="slack"` and `address="C123"`;
- `enqueue_result()` creates a pending fake Slack delivery event without any
  Slack-specific branch;
- `DeliveryDispatcher` dispatches that event with a registry containing the fake
  adapter and marks it delivered;
- existing local, origin, and webhook tests continue to pass, with expectations
  adjusted only where they depended on pre-registry branching.

## Non-Goals

This change will not implement real Slack, Email, Discord, or gateway delivery.
It will not redesign scheduler leadership, run storage, missed-run policy,
timeouts, worker queues, or cron management commands. Those are separate cron
system gaps and should build on this cleaner delivery boundary.
