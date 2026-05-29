# Cron Delivery Semantics, Registry, And Origin Identity Design

## Goal

Define the next delivery architecture stage for cron jobs after the registry
boundary refactor. This design covers local/origin semantics, registry
extensibility, status/help visibility, origin identity sources, cleanup of old
target APIs, and tests that prove the delivery model can grow without adding
new core branches.

The implementation should be phased. The design is intentionally broad enough
to keep the phases compatible, but each phase should be independently testable.

## Current State

The current project already has these pieces in place:

- `DeliveryAdapter`, `DeliveryRegistry`, and `DeliveryDispatcher` exist.
- `enqueue_result()` validates through `DeliveryRegistry.validate_targets()`.
- `parse_target()` has been removed from `cron/delivery.py`.
- `parse_delivery_targets()` supports comma-separated delivery targets and
  `platform:chat_id[:thread_id]`.
- Tests prove a fake `slack` adapter can create, enqueue, dispatch, and mark a
  delivery event without adding Slack-specific branches to core delivery code.
- `CronJobInput.deliver` already describes `local`, `origin`, `webhook:<url>`,
  and `platform:chat_id[:thread_id]`.
- `cron status` prints registered adapter keys from the default registry.

Remaining ambiguity:

- `local` has an adapter, but local delivery is effectively synchronous because
  cron output is already saved before delivery events are queued.
- `origin` has an adapter, but the default dispatcher deliberately does not
  claim origin events. Origin currently behaves like a poll/host-bridge target,
  not like an actively dispatched adapter.
- `default_delivery_registry()` still hand-registers built-ins and is the only
  practical registry construction path.
- Status/help output is not fully tied to the same registry extension point that
  future adapters will use.
- Runtime origin identity still starts from LangGraph thread id and does not
  reliably distinguish CLI sessions, gateway chats, or web sessions.

Hermes is a semantic reference for platform/chat/thread origin modeling and
target resolution. Its monolithic delivery router should not be copied because
this project already has a better durable event and adapter registry model.

## Design Principles

Delivery targets should be understood through three separate concepts:

- **Validation:** Is the configured target syntactically and semantically valid
  for the available registry?
- **Durable event state:** What happened to the output after a run?
- **Dispatch ownership:** Which component is responsible for moving a pending
  event to delivered, failed, or dead?

Adapters should own target-specific validation and active dispatch behavior.
Core cron code should not grow target-specific branches for Slack, Email,
Discord, or future platform adapters.

Origin identity should be stored once in a stable shape and copied into delivery
targets/events. It should not be inferred later from a single thread id when
platform/chat/session information was available at creation time.

## Local Semantics

`local` means the cron output has been written to the local output path. It is a
synchronous completion marker, not an external delivery action.

The intended behavior is:

- output is saved before `enqueue_result()` runs;
- a local delivery event may still be recorded for audit/run-status purposes;
- the local event is immediately `delivered`;
- the default dispatcher does not need to claim local events;
- local failures are output-save failures, not delivery adapter failures.

The existing `LocalDeliveryAdapter.deliver()` should not be treated as the
normal local path. The implementation can either remove `LocalDeliveryAdapter`
or keep it as a validation-only/compatibility adapter, but the code and tests
must make clear that local delivery is synchronously complete when the event is
created.

## Origin Semantics

`origin` means the result should return to the source context that created the
job. In the current architecture this is a poll/host-bridge delivery mode, not a
normal active dispatch adapter.

The intended behavior is:

- `origin` validates through the registry using `DeliveryIdentity`;
- origin events are persisted as pending work for the origin poller or host
  bridge;
- the default dispatcher does not claim origin events;
- status and logs describe this as waiting for origin poll/bridge pickup, not as
  an ordinary pending webhook-style dispatch;
- `OriginDeliveryAdapter.deliver()` should either be removed or made explicitly
  non-dispatching so callers do not mistake it for a live gateway delivery path.

Future live gateway delivery can be added as a separate adapter/mode. It should
not be implied by the current `origin` adapter.

## Registry Extensibility

`default_delivery_registry()` should not remain the only way to assemble
delivery adapters. Introduce a registry construction layer that supports
built-ins plus in-process extension hooks.

Recommended shape:

- keep `DeliveryRegistry.register(adapter)`;
- add a small module-level extension API, such as
  `register_delivery_adapter_factory(factory)`;
- add `build_delivery_registry(webhook_sender=None, extra_adapters=None)` as the
  canonical construction path;
- keep `default_delivery_registry()` as a compatibility alias that calls
  `build_delivery_registry()`;
- status, doctor, validation, enqueue, and dispatcher should all use the same
  construction path unless tests inject a custom registry.

Factories should receive enough context to build adapters that need optional
dependencies, but this phase should not implement Python package entry points or
real provider integrations. A fake Slack adapter remains the proof of
extensibility.

## Tool And Status UX

Tool descriptions and status/help output should reflect the registry rather than
hardcoded assumptions.

Required behavior:

- `CronJobInput.deliver` continues to document:
  `local`, `origin`, `webhook:<url>`, and `platform:chat_id[:thread_id]`;
- validation failures for unsupported targets should include known adapter keys;
- `cron status` should display adapter keys from the canonical registry;
- `cron doctor` should verify required built-ins are present and list registered
  adapters;
- origin pending delivery should be displayed as origin poll/bridge pending in
  status/log-oriented output.

This phase does not need a new interactive help system. It only needs the
existing command/tool surfaces to read from the registry and use clearer
delivery-state wording.

## Origin Identity Model

`DeliveryIdentity` remains the persisted origin model:

```python
{
    "source_type": "cli" | "gateway" | "web",
    "platform": str | None,
    "chat_id": str | None,
    "thread_id": str | None,
    "session_id": str | None,
    "display_name": str | None,
}
```

Creation sources:

- CLI REPL:
  - `source_type="cli"`
  - `session_id=<cli session id>`
  - `thread_id=<langgraph thread id or session id>`
- Gateway chat:
  - `source_type="gateway"`
  - `platform=<gateway platform>`
  - `chat_id=<stable chat/channel id>`
  - `thread_id=<platform thread/topic id if present>`
  - `session_id=<gateway session id if available>`
- Web session:
  - `source_type="web"`
  - `session_id=<web session id>`
  - optional `chat_id` and `thread_id` if the web layer has stable equivalents

`RuntimeContext` should be extended or paired with a new helper that can extract
this identity from runtime/config metadata. The cronjob tool should call that
helper when `deliver` is omitted or mentions `origin`. It should not construct
origin by treating every active thread as a CLI thread.

Backward compatibility:

- old jobs with only `origin.thread_id` should still load as CLI-like identity;
- `DeliveryIdentity.from_job_origin()` can keep fallback inference for legacy
  data, but new writes should include explicit `source_type`.

## Cleanup Constraints

`parse_target()` has already been removed and should not be reintroduced.
Target parsing should remain in `cron.delivery_targets.parse_delivery_targets()`
and validation should remain behind `DeliveryRegistry.validate_targets()`.

Any local/origin special handling should be explicit semantic handling, not a
return to one-off target parsing in `cron/delivery.py`.

## Phased Implementation

### Phase 1: Local And Origin Semantics

- Decide whether `LocalDeliveryAdapter` remains validation-only or is removed.
- Ensure local events are recorded as delivered and are not dispatcher work.
- Make origin pending state visible as origin poll/bridge pending.
- Make `OriginDeliveryAdapter` validation-only or explicitly non-dispatching.
- Add tests for local synchronous completion and origin non-claim behavior.

### Phase 2: Registry Construction And Introspection

- Add the canonical registry builder and in-process adapter factory hooks.
- Keep `default_delivery_registry()` as compatibility wrapper.
- Update status/doctor/tool validation to read adapter keys from the canonical
  registry.
- Add tests showing a registered fake Slack adapter appears in registry
  introspection and works without core delivery branches.

### Phase 3: Tool UX And Error Messages

- Improve unsupported target errors with known adapter keys.
- Ensure `CronJobInput.deliver` and any CLI help remain accurate.
- Update status/log wording for origin poll pending.
- Add command/tool tests for adapter-key visibility and clearer errors.

### Phase 4: Origin Identity Sources

- Extend `RuntimeContext` or add a dedicated origin identity extractor.
- Wire CLI cron creation to produce explicit CLI identity.
- Wire gateway and web runtime/config metadata to stable gateway/web identities.
- Persist and restore origin identity in jobs, delivery targets, and delivery
  events.
- Add tests for CLI, gateway, and web origin identity creation.

## Testing Strategy

Required coverage:

- fake adapter:
  - registered `slack` adapter allows `slack:C123` create/enqueue/dispatch;
  - adapter appears in status/doctor/introspection;
  - unsupported platform errors include known adapter keys;
- local:
  - local result records delivered event immediately;
  - default dispatcher does not need to claim local events;
  - local semantics do not rely on provider-specific branches;
- origin:
  - origin event remains pending for poll/bridge pickup;
  - default dispatcher does not claim origin events;
  - status/log output labels origin pending clearly;
- identity:
  - CLI identity persists `source_type`, `session_id`, and `thread_id`;
  - gateway identity persists `source_type`, `platform`, `chat_id`, and optional
    `thread_id/session_id`;
  - web identity persists `source_type` and `session_id`;
  - legacy `origin.thread_id` jobs still load.

## Non-Goals

This design does not implement real Slack, Email, Discord, Matrix, or gateway
delivery. It does not add Python package entry point discovery. It does not
redesign scheduler leadership, missed-run policy, timeout policy, run storage,
or worker queues.

Those features should build on the clarified delivery semantics and registry
extension points defined here.
