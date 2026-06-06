# Feishu Gateway Outbound Design

## Goal

Build the firstreference implementation-like gateway foundation by adding a lightweight outbound gateway core and a Feishu application bot adapter that cron delivery can use to send text messages to Feishu chats.

This milestone intentionally focuses on outbound delivery and cron integration. It does not implement inbound Feishu event callbacks, agent conversations from Feishu, personal WeChat, Enterprise WeChat application messages, cards, rich text, files, or a standalone gateway service manager.

## Context

The project already has a reliable cron runtime and delivery state machine:

- `CronService` runs scheduler ticks under a leader lease.
- `scheduler.tick()` processes delivery maintenance on every tick, even when no jobs are due.
- `DeliveryDispatcher` claims due events, invokes delivery adapters, and syncs run/job delivery status.
- Delivery targets can store resolved addresses so runtime delivery remains stable when environment variables change.
- `wecom` group robot delivery now proves the adapter/retry/doctor/E2E pattern.

Reference implementation has a richer gateway model: platform adapters, connected platform metadata, session origin identity, live adapter sends, and standalone gateway services. This project should not copy that whole stack in one step. The first step is a small outbound gateway core that can support Feishu now and later support inbound messages, personal WeChat bridges, and Enterprise WeChat application messages.

## Scope

In scope:

- Add a `gateway/` package with outbound contracts and registry.
- Add a Feishu platform adapter for application bot text messages.
- Use `FEISHU_APP_ID` and `FEISHU_APP_SECRET` from environment variables.
- Fetch and cache `tenant_access_token` in memory.
- Send text messages to Feishu `chat_id` targets.
- Add a cron `feishu` delivery adapter that bridges cron delivery events to the gateway registry.
- Support `deliver="feishu:<chat_id>"`.
- Add cron doctor validation for active Feishu delivery jobs.
- Add unit, integration, and service E2E tests using fake HTTP senders.

Out of scope:

- `deliver="feishu"` default home channel.
- Sending to `open_id` or user private chats.
- Feishu inbound event subscription and callback HTTP server.
- Feishu rich text `post`, interactive cards, files, images, or reactions.
- A standalone `agent gateway` CLI or gateway service installation.
- Personal WeChat gateway.
- Enterprise WeChat application messages.

## Architecture

Add a new lightweight `gateway/` package.

### `gateway/contracts.py`

Defines platform-neutral outbound interfaces:

- `PlatformMessageTarget`
  - `platform`
  - `target_type`
  - `target_id`
  - optional `thread_id`
- `OutboundMessage`
  - `text`
  - optional metadata
- `SendResult`
  - `ok`
  - `error`
  - `retryable`
- `PlatformAdapter` protocol
  - `validate_target(target) -> SendResult`
  - `send_text(target, message) -> SendResult`

### `gateway/registry.py`

Owns adapter registration and lookup:

- `GatewayRegistry.register(adapter)`
- `GatewayRegistry.get(platform)`
- `GatewayRegistry.platform_keys()`
- `default_gateway_registry()`

The default registry should register the Feishu platform adapter.

### `gateway/platforms/feishu.py`

Owns all Feishu application bot details:

- Reads `FEISHU_APP_ID`.
- Reads `FEISHU_APP_SECRET`.
- Fetches `tenant_access_token`.
- Caches the token in process memory until shortly before expiry.
- Sends text messages to `chat_id`.
- Converts Feishu HTTP/API responses into `SendResult`.

The adapter should accept an injectable HTTP sender for deterministic tests.

### `cron/delivery_adapters.py`

Add `FeishuDeliveryAdapter` as a cron bridge:

- `key = "feishu"`.
- `active_dispatch = True`.
- Validate `feishu:<chat_id>` target semantics.
- Format cron result as plain text.
- Call `gateway.registry.default_gateway_registry().get("feishu").send_text(...)`.
- Convert `SendResult` into cron `DeliveryResult`.

Cron delivery should not know Feishu token or OpenAPI details.

## Delivery Target Semantics

MVP supports only explicit chat targets:

```text
feishu:<chat_id>
```

Parsing result:

- `target_type = "platform"`
- `adapter_key = "feishu"`
- `address = "<chat_id>"`

`deliver="feishu"` is not supported in this milestone. It should fail validation with a clear error so users do not assume a hidden default home channel exists.

Stored target behavior should match current cron delivery semantics: once a job is created or updated, the resolved delivery target is stored with the job and used at runtime.

## Cron Data Flow

1. User creates a job:

   ```bash
   agent cron create "every 30m" "generate daily report" --deliver "feishu:<chat_id>"
   ```

2. `cron.delivery_targets` parses the target.
3. `FeishuDeliveryAdapter.validate()` checks:
   - chat_id is non-empty
   - Feishu gateway adapter is registered
   - `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are present
4. After a run completes, existing `enqueue_result()` creates a delivery event.
5. Each scheduler tick processes due delivery events.
6. `FeishuDeliveryAdapter.deliver()` formats text and calls the gateway adapter.
7. Gateway Feishu adapter sends the message through Feishu OpenAPI.
8. Existing delivery state transitions mark the event/run/job delivered, retrying, or dead.

## Origin Return Path

The MVP does not implement Feishu inbound events, so it cannot automatically create Feishu-origin cron jobs.

The data model should remain compatible with a future inbound gateway:

```json
{
  "origin": {
    "source_type": "gateway",
    "platform": "feishu",
    "chat_id": "<chat_id>",
    "thread_id": null,
    "display_name": "..."
  },
  "deliver": "origin"
}
```

Current MVP behavior:

- Active Feishu sending uses `deliver="feishu:<chat_id>"`.
- `deliver="origin"` keeps the existing origin semantics and is not remapped to Feishu yet.
- Future inbound gateway work can translate Feishu origin identity into the stable `feishu:<chat_id>` target or extend `OriginDeliveryAdapter` through a host bridge.

## Feishu Authentication

Configuration comes from environment variables:

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

The Feishu adapter should:

- Fetch `tenant_access_token` before sending.
- Cache token and expiry in memory.
- Refresh token before it expires.
- Avoid writing tokens to disk.
- Return non-retryable errors for missing or invalid app credentials.
- Return retryable errors for network, timeout, 429, or 5xx token failures.

## Feishu Send Format

The MVP sends text messages only.

Request intent:

- endpoint: Feishu message create API
- `receive_id_type=chat_id`
- `receive_id=<chat_id>`
- `msg_type=text`
- `content` is a JSON string containing `{"text": "..."}`.

Cron text should include:

- cron job name
- job ID
- run ID when available
- status
- final response or error
- output path when available

Long content should be truncated while keeping the output path in the header.

## Error Mapping

Gateway `SendResult` maps into cron `DeliveryResult`.

Retryable:

- network exception
- timeout
- HTTP 408
- HTTP 429
- HTTP 5xx
- known temporary Feishu API codes

Non-retryable:

- missing `FEISHU_APP_ID`
- missing `FEISHU_APP_SECRET`
- invalid `app_id` or `app_secret`
- invalid `chat_id`
- permission denied
- malformed request
- unsupported target type

Success:

- Feishu API JSON `code == 0`

Errors should include enough context for doctor and delivery inspection:

```text
Feishu token failed: code=<code> msg=<msg>
Feishu send failed: code=<code> msg=<msg>
HTTP 429: <body>
```

## Doctor And Observability

`agent cron doctor` should:

- List `feishu` in delivery adapters.
- For active `feishu:<chat_id>` jobs:
  - validate chat_id is non-empty
  - validate `FEISHU_APP_ID` exists
  - validate `FEISHU_APP_SECRET` exists
  - perform a token smoke check without sending a message
- Never send a real Feishu message during doctor.

Severity:

- Missing env or malformed target: fail.
- Token smoke failure caused by invalid credentials: fail.
- Token smoke temporary/network failure: warn.

Existing commands remain the main observability surface:

- `agent cron doctor`
- `agent cron status`
- `agent cron deliveries`
- `agent cron runs <job_id>`
- service logs

## Testing

### Gateway Core

- registry registers and returns `feishu`.
- unknown platform returns a clear missing-adapter result.
- contracts are simple serializable dataclasses where practical.

### Feishu Platform Adapter

Use fake HTTP sender:

- missing app id fails validation.
- missing app secret fails validation.
- token success is cached.
- second send before expiry does not fetch token again.
- token network/5xx failure is retryable.
- token credential failure is non-retryable.
- send text payload uses `receive_id_type=chat_id`.
- send text payload uses `msg_type=text`.
- send text `content` is a JSON string.
- send API `code == 0` succeeds.
- send API temporary/rate-limit failure is retryable.
- send API permission/chat/parameter failure is non-retryable.

### Cron Delivery

- `deliver="feishu:<chat_id>"` creates a stored platform target.
- `deliver="feishu"` fails with an explicit unsupported-default error.
- successful run enqueues and delivers a Feishu delivery event.
- retryable Feishu failure is retried on a later tick even with no due jobs.
- permanent Feishu failure becomes dead-letter with clear error.

### Doctor And E2E

- doctor lists `feishu`.
- active Feishu job missing env fails.
- active Feishu job with token smoke invalid credentials fails.
- active Feishu job with token smoke temporary/network failure warns.
- service E2E proves:
  - create job
  - service tick executes it
  - run record is saved
  - output file is saved
  - Feishu text payload is sent through fake sender
  - delivery status becomes delivered
  - no-due tick retries pending Feishu delivery

## Acceptance Criteria

- A user can configure `FEISHU_APP_ID` and `FEISHU_APP_SECRET`.
- A user can create a cron job with `deliver="feishu:<chat_id>"`.
- Cron service can deliver job results to Feishu as text through the gateway adapter.
- Retryable Feishu failures are recovered by the existing delivery retry loop.
- Permanent Feishu failures become dead-letter events with actionable errors.
- `agent cron doctor` can detect missing Feishu configuration for active Feishu jobs.
- The implementation establishes reusable gateway contracts for later inbound Feishu, personal WeChat, and Enterprise WeChat application work.
