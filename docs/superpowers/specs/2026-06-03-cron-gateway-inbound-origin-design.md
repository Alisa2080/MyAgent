# Cron Gateway Inbound And Origin Return Design

## Goal

Extend the current cron system from outbound scheduled notifications into a two-service gateway architecture that can receive messaging-platform events, maintain gateway sessions, create or continue agent work from those events, and deliver cron results back to the originating platform.

The selected architecture keeps `cron service` and `gateway service` independent:

- `cron service` remains responsible for scheduling, running jobs, delivery events, retries, dead letters, and service heartbeat.
- `gateway service` becomes responsible for HTTP callbacks, inbound platform adapters, gateway sessions, platform event dispatch, and origin routing.

This design builds on the existing cron reliability model instead of copying Hermes' gateway-owned cron tick model.

## Context

The project already has a mature cron runtime:

- `CronService` runs scheduler ticks under a leader lease.
- `DeliveryDispatcher` claims due delivery events and maps adapter results into delivered, retrying, or dead states.
- Delivery targets are resolved and stored with jobs so runtime behavior stays stable.
- `OriginDeliveryAdapter` currently exists as an inactive adapter for host bridge pickup.
- `gateway/` already contains lightweight outbound contracts, a registry, and a Feishu outbound adapter.
- Feishu outbound cron delivery already proves the gateway adapter can be used by cron without embedding Feishu OpenAPI details in cron.

Hermes provides useful reference behavior:

- Platform messages are normalized before dispatch.
- Sessions carry origin identity such as platform, chat ID, and thread ID.
- Cron jobs created from a platform can use `deliver=origin`.
- Platform adapters can be used for live outbound sends.

The current project should adopt those semantics while preserving its stronger cron service and delivery state machine boundaries.

## Scope

In scope:

- Add a standalone gateway service runtime.
- Add a small HTTP callback server owned by gateway service.
- Add platform-neutral inbound event contracts.
- Extend gateway adapter contracts from outbound-only to bidirectional where supported.
- Add a gateway session store for platform-origin sessions and message history.
- Support Feishu inbound event subscription callbacks for text messages.
- Support origin routing from Feishu-origin cron jobs back to Feishu.
- Keep cron delivery retries and dead-letter handling in the existing cron delivery system.
- Add CLI/status/doctor coverage for gateway service and Feishu callback configuration.
- Add unit, integration, and E2E tests with fake HTTP senders and fake callback requests.

Out of scope for the first complete pass:

- Merging gateway service and cron service into one daemon.
- Personal WeChat, Enterprise WeChat application inbound events, Telegram, Slack, Discord, or other platform inbound adapters.
- Feishu rich text, files, images, cards, reactions, or message edits.
- Public tunnel provisioning or automatic Feishu app configuration.
- Multi-user access control beyond validating platform sender identity and preserving origin metadata.
- Replacing the existing cron delivery event store.

## Architecture

### Service Boundary

`cron service` and `gateway service` run as separate processes.

`cron service` continues to call:

- `scheduler.tick()`
- `DeliveryDispatcher`
- registered cron delivery adapters
- existing service state and heartbeat writers

`gateway service` owns:

- HTTP callback listener
- platform adapter registry
- inbound event normalization
- gateway session lookup and persistence
- dispatch from inbound events into the agent/session layer
- optional direct platform sends requested by origin delivery

The two services communicate through durable state and shared contracts, not by controlling each other's process lifecycle.

### Gateway Contracts

Extend `gateway/contracts.py` with inbound concepts while preserving the current outbound model.

Core contracts:

- `PlatformMessageTarget`
  - `platform`
  - `target_type`
  - `target_id`
  - optional `thread_id`
- `OutboundMessage`
  - `text`
  - metadata
- `SendResult`
  - `ok`
  - `error`
  - `retryable`
- `InboundEvent`
  - `platform`
  - `event_id`
  - `event_type`
  - `chat_id`
  - optional `thread_id`
  - optional `sender_id`
  - optional `sender_name`
  - `text`
  - `timestamp`
  - `raw`
- `InboundParseResult`
  - `ok`
  - optional `event`
  - optional `response_body`
  - optional `status_code`
  - optional `error`
- `PlatformAdapter`
  - outbound methods remain available for platforms that can send.
  - inbound-capable adapters add callback parsing and validation methods.

Outbound-only adapters can continue to satisfy the existing protocol by not implementing inbound behavior.

### Gateway Service

Add a `GatewayService` runtime that:

1. Builds the gateway registry.
2. Starts the callback HTTP server.
3. Loads the gateway session store.
4. Accepts normalized inbound events from adapters.
5. Deduplicates events by platform and event ID.
6. Resolves or creates a gateway session.
7. Dispatches the text message into the agent/session layer.
8. Sends any immediate agent response through the platform adapter.

Gateway service should write its own status and heartbeat files. It should not write cron service status.

### HTTP Callback Server

The callback server should be deliberately small and testable.

Routes:

- `GET /health`
  - returns service health and configured platform keys.
- `POST /callback/<platform>`
  - reads JSON body.
  - forwards headers and body to the registered platform adapter.
  - handles platform challenge responses.
  - returns adapter-provided response for verification events.
  - queues or dispatches normalized message events.

The server should accept injectable handlers so tests do not need a real listening socket for most cases. Socket-level E2E coverage can be limited to health and a fake callback route.

### Gateway Session Store

Extend the current session persistence model with gateway-specific tables instead of overloading CLI-only session fields.

Required stored data:

- `session_id`
- `platform`
- `chat_id`
- `thread_id`
- `sender_id`
- `sender_name`
- `created_at`
- `updated_at`
- `status`
- `last_event_id`
- `last_message_preview`

Message history should record:

- `message_id`
- `session_id`
- `direction` (`inbound` or `outbound`)
- `platform`
- `event_id`
- `text`
- `created_at`
- raw metadata where useful

Session identity key:

```text
platform + chat_id + thread_id
```

If `thread_id` is absent, the chat-level session is used. This keeps Feishu group chats stable while allowing future threaded platforms to split conversations.

### Feishu Adapter

Extend `gateway/platforms/feishu.py` from outbound-only to bidirectional.

Inbound responsibilities:

- Handle Feishu callback challenge events.
- Validate callback token or signature according to configured environment.
- Parse text message events into `InboundEvent`.
- Preserve `open_message_id`, `chat_id`, `thread_id` when available, sender ID, sender display name, and raw event payload.
- Ignore unsupported event types with a clear non-error response when Feishu expects acknowledgement.

Outbound responsibilities remain:

- Fetch and cache tenant access tokens.
- Send text messages to `chat_id`.
- Map Feishu HTTP/API failures to `SendResult`.

Configuration:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_CALLBACK_TOKEN` for event verification.
- Optional `FEISHU_CALLBACK_ENCRYPT_KEY` can be added only when encrypted callbacks are implemented.

Encrypted Feishu callbacks are not part of the first inbound milestone. If an encrypted callback is received before support exists, the adapter should return a clear unsupported configuration error.

### Inbound Dispatch

Gateway inbound dispatch should be session-scoped and serialized per session.

For each inbound text event:

1. Deduplicate by `(platform, event_id)`.
2. Resolve or create a gateway session.
3. Store inbound message.
4. Build session context with origin:

   ```json
   {
     "source_type": "gateway",
     "platform": "feishu",
     "chat_id": "<chat_id>",
     "thread_id": "<thread_id-or-null>",
     "sender_id": "<sender_id-or-null>",
     "display_name": "<sender-name-or-null>",
     "session_id": "<gateway-session-id>"
   }
   ```

5. Dispatch to the agent runner using existing session and workspace conventions.
6. Store outbound response.
7. Send outbound response through the same platform adapter.

The first implementation should process one event at a time per session. Global parallelism is acceptable across unrelated sessions.

### Origin Return Path

Cron jobs created or updated from a gateway-origin session should carry origin metadata.

For Feishu-origin jobs:

```json
{
  "origin": {
    "source_type": "gateway",
    "platform": "feishu",
    "chat_id": "<chat_id>",
    "thread_id": "<thread_id-or-null>",
    "sender_id": "<sender_id-or-null>",
    "display_name": "<display-name-or-null>",
    "session_id": "<gateway-session-id>"
  },
  "deliver": "origin"
}
```

`OriginDeliveryAdapter` should become actively dispatchable when the origin target can be resolved through a registered gateway adapter.

Origin delivery flow:

1. Cron run completes and enqueues a delivery event as it does today.
2. `DeliveryDispatcher` claims the due event.
3. `OriginDeliveryAdapter` reads stored origin metadata.
4. It converts the origin into `PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id=chat_id, thread_id=thread_id)`.
5. It sends through `gateway.registry.default_gateway_registry().get("feishu")`.
6. It maps `SendResult` into existing `DeliveryResult`.
7. Existing retry and dead-letter behavior remains unchanged.

Cron should not need to know Feishu callback formats, callback tokens, or session store internals.

## Development Phases

### Phase 1: Gateway Core Contracts

Add inbound event and adapter contracts. Update gateway registry only as needed to support bidirectional adapters.

Acceptance criteria:

- A fake adapter can be registered.
- A fake inbound event can be represented without Feishu-specific fields.
- Existing Feishu outbound delivery tests continue to pass.

### Phase 2: HTTP Callback Server

Add the gateway-owned HTTP server and a fake platform callback route.

Acceptance criteria:

- `GET /health` returns healthy state.
- `POST /callback/fake` can produce an `InboundEvent`.
- Duplicate event IDs are not dispatched twice.
- Unsupported platforms return a clear 404 or 400 response.

### Phase 3: Gateway Session Store

Add gateway session and message persistence.

Acceptance criteria:

- Same platform/chat/thread resolves to the same session.
- Different thread IDs resolve to different sessions.
- Inbound and outbound messages are persisted in order.
- CLI session store behavior remains unchanged.

### Phase 4: Feishu Inbound Adapter

Implement Feishu callback parsing for challenge and text message events.

Acceptance criteria:

- Feishu challenge receives the expected response.
- Valid text event becomes an `InboundEvent`.
- Invalid token/signature is rejected.
- Unsupported event types are acknowledged or ignored according to Feishu callback expectations.

### Phase 5: Gateway Inbound Dispatch

Connect inbound events to the agent/session layer.

Acceptance criteria:

- A fake inbound message creates or resumes a gateway session.
- The agent receives origin context.
- A text response is sent through a fake outbound adapter.
- Concurrent events for the same session are serialized.

### Phase 6: Origin Delivery

Upgrade origin delivery to actively send through gateway adapters when origin metadata is present.

Acceptance criteria:

- `deliver="origin"` on a Feishu-origin cron job sends to the original chat.
- Retryable Feishu failures stay in the existing delivery retry path.
- Permanent Feishu errors become dead-letter events with clear errors.
- Local and non-gateway origin behavior remains compatible.

### Phase 7: Gateway CLI And Service Lifecycle

Add gateway service commands and status reporting.

Acceptance criteria:

- `agent gateway serve` runs the callback server in foreground.
- `agent gateway status` reports process status, heartbeat, host, port, and registered platforms.
- Service install/start/stop can reuse the cron service platform patterns without sharing cron status files.
- `agent cron status` remains focused on cron.

### Phase 8: E2E And Doctor

Add end-to-end tests and configuration diagnostics.

Acceptance criteria:

- Fake Feishu callback creates a gateway session.
- A Feishu-origin cron job can be created with `deliver="origin"`.
- Cron service run completes the job and sends the result back through a fake Feishu sender.
- Doctor reports missing Feishu app credentials, missing callback token, invalid callback configuration, and gateway service not running.

## Error Handling

Gateway callback errors:

- Invalid JSON returns 400.
- Unknown platform returns 404.
- Invalid platform token/signature returns 401 or 403.
- Unsupported encrypted callback returns 400 with a clear configuration error.
- Adapter parse errors are logged without crashing the service.

Inbound dispatch errors:

- Duplicate event IDs are acknowledged and not reprocessed.
- Session store failures return a controlled gateway error and do not run the agent.
- Agent execution failures should be sent back to the origin when possible and recorded in session history.

Origin delivery errors:

- Network, timeout, 408, 429, and 5xx platform failures are retryable.
- Missing origin chat ID, unsupported platform, or invalid credentials are non-retryable.
- Feishu API temporary error codes are retryable.
- Feishu permanent API errors are dead-lettered.

## Testing

Unit tests:

- Contract construction and adapter protocol behavior.
- Gateway registry with inbound-capable and outbound-only adapters.
- Callback server routing with fake adapters.
- Gateway session identity and message persistence.
- Feishu challenge parsing.
- Feishu text event normalization.
- Origin metadata to `PlatformMessageTarget` conversion.
- `OriginDeliveryAdapter` result mapping.

Integration tests:

- Fake callback request through HTTP handler to session store.
- Fake inbound event through dispatch to fake outbound adapter.
- Feishu-origin cron job delivery through existing `DeliveryDispatcher`.

E2E tests:

- Start gateway server in test mode.
- Submit fake Feishu callback.
- Create or simulate a cron job with gateway origin and `deliver="origin"`.
- Run cron service once.
- Verify delivery event is marked delivered and fake Feishu sender received the result.

## Acceptance Criteria

- Gateway service and cron service can be run independently.
- Feishu inbound text events can create or resume gateway sessions.
- Gateway sessions persist origin identity and message history.
- Cron jobs created from a Feishu-origin session can use `deliver="origin"`.
- Origin delivery reuses the existing cron delivery state machine.
- Feishu outbound and inbound logic stays inside the gateway adapter layer.
- Existing cron delivery targets, including local, webhook, wecom, and explicit Feishu delivery, remain compatible.
- Doctor and status commands make service and Feishu configuration failures visible before runtime.
