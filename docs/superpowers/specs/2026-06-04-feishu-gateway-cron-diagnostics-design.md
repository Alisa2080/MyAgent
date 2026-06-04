# Feishu Gateway Cron Diagnostics Design

Date: 2026-06-04

## Summary

Keep the Feishu WebSocket gateway and cron scheduler as separate services, but make the dependency visible at runtime. Starting `gateway feishu-ws` should continue even when cron is not running, while printing a clear warning that scheduled jobs will not execute automatically. `agent_cli doctor` should report the combined gateway/cron/Feishu delivery health needed for the Hermes-style experience where interactive replies and scheduled reports both reach the same Feishu chat. Feishu WebSocket event handling should log enough metadata to distinguish "Feishu did not push an event" from "the local gateway received and ignored an event".

## Goals

- Preserve the current two-service architecture: gateway handles inbound/outbound messaging, cron service handles scheduled execution.
- Warn, but do not block, when `gateway feishu-ws` starts without a running cron service.
- Extend unified doctor output with gateway, cron, Feishu token, cron delivery, inbox, and delivery queue diagnostics.
- Add Feishu WS event diagnostic logging for received, enqueued, ignored, and handler-error cases.
- Keep existing public APIs compatible unless a new helper is internal-only.

## Non-Goals

- Do not auto-start or install cron service from `gateway feishu-ws`.
- Do not embed a Hermes-style cron ticker inside the gateway process.
- Do not change cron job creation semantics or delivery target parsing.
- Do not change Feishu message send/reply behavior in this scope.
- Do not add a separate `gateway doctor` command unless unified doctor cannot cleanly express the checks.

## User-Facing Behavior

When `python3 -m agent_cli.main gateway feishu-ws` starts and cron service is not healthy, it prints a warning before the "listening" line and continues:

```text
WARN Cron service is not running; scheduled cron jobs will not run automatically.
Run: python3 -m agent_cli.main cron service start
```

If the platform does not support managed cron service, the warning points to foreground scheduling:

```text
WARN Cron service is not available on this platform; scheduled cron jobs will not run automatically.
Run: python3 -m agent_cli.main cron serve
```

The command exit code remains `0` if the gateway itself starts successfully.

## Doctor Checks

Extend `agent_cli doctor` using the existing `HealthCheck` style:

- `Gateway Service`: reads gateway status, reports whether it is fresh and running, and includes transport. Warn if not running or if transport is not `feishu-ws` for the Feishu WebSocket path.
- `Cron Service`: uses `cron.service_manager.compose_service_status()`. OK when the service is active, heartbeat is fresh, and `process_state` is `running`. Warn for inactive/stale/non-running states. Include leader state, last heartbeat, last tick summary, and last error when available.
- `Feishu Token`: uses the registered Feishu gateway adapter `token_smoke()` to validate that `FEISHU_APP_ID` and `FEISHU_APP_SECRET` can obtain a tenant token. Missing env remains WARN, token API failure is WARN because it blocks Feishu send but not local CLI operation.
- `Cron Feishu Delivery`: verifies that the cron delivery registry has active `origin` and `feishu` adapters, the gateway registry has the `feishu` platform adapter, and Feishu target validation can run with current env.
- `Gateway Inbox`: reads `<cli_home>/gateway/gateway.sqlite` if present and reports `pending`, `processing`, `failed`, `dead`, and `succeeded`. Warn if `failed` or `dead` is non-zero.
- `Cron Delivery Queue`: reads `DeliveryStore().stats()` and reports `pending`, `delivering`, `failed`, `dead`, and `delivered`. Warn if `failed` or `dead` is non-zero. If `pending` is non-zero while cron service is unhealthy, mention that cron delivery dispatch may not be running.

Doctor remains best-effort: inspection failures are WARN unless they indicate core CLI storage is unusable, matching the existing doctor behavior.

## Feishu WS Diagnostic Logging

Add module-level logging in `gateway/transports/feishu_ws.py`.

Introduce an internal helper:

```python
normalize_feishu_ws_event_with_reason(payload) -> tuple[InboundEvent | None, str | None]
```

`normalize_feishu_ws_event(payload)` remains as the compatibility wrapper returning only the event.

Ignored reason values should be stable short strings:

- `unsupported_event_type`
- `unsupported_message_type`
- `missing_chat_id`
- `missing_message_id`
- `missing_event_id`
- `empty_text`
- `malformed_payload`

`on_message` logs:

- INFO on received callback with `event_type`, `event_id`, `chat_id`, `message_id`, and `message_type` when available.
- INFO on successful enqueue with `event_id` and inbox id.
- WARNING on ignored event with the ignored reason and available event metadata.
- EXCEPTION on JSON marshal/parse or unexpected handler errors.

The logs must not include message text by default. Event IDs, chat IDs, message IDs, event type, and message type are acceptable diagnostics.

## Data Flow

1. User sends a Feishu message.
2. Feishu WS SDK invokes `on_message`.
3. `on_message` logs the received metadata.
4. Normalization either returns an `InboundEvent` or a reason.
5. Valid events are enqueued in `GatewayInboxStore`.
6. `GatewayInboxWorker` dispatches the event to `GatewayService`.
7. Agent may create a cron job with gateway origin.
8. Cron service independently ticks, runs due jobs, enqueues delivery, and dispatches origin delivery through the Feishu gateway adapter.

This design keeps interactive reply and scheduled report paths independent after cron job creation.

## Error Handling

- Cron service startup warning never raises and never changes gateway exit behavior.
- Doctor checks catch local inspection and adapter smoke exceptions and report WARN with the exception summary.
- Feishu WS handler logs exceptions before re-raising or allowing SDK error handling to continue.
- Normalization failures do not raise for expected ignored cases; they return a reason.

## Testing

Add focused tests:

- `gateway_feishu_ws` prints cron service WARN and still calls `serve_feishu_ws_gateway()`.
- Healthy cron service status suppresses the warning.
- Doctor reports gateway transport, cron service state, inbox stats, and delivery queue stats.
- Doctor warns on inbox failed/dead and delivery failed/dead.
- Feishu token smoke OK/WARN paths are covered with fake adapters.
- Feishu WS text event logs received and enqueued metadata.
- Feishu WS ignored cases log stable reasons and do not enqueue.
- Existing gateway, cron, and doctor tests continue to pass.

## Acceptance Criteria

- Running only `gateway feishu-ws` makes the missing cron runtime explicit without blocking real-time chat.
- `agent_cli doctor` can identify whether the Feishu gateway, cron scheduler, Feishu sending, gateway inbox, and cron delivery queue are healthy.
- When a Feishu message gets no response, logs show whether no SDK callback arrived, a callback arrived and was ignored, or a callback was enqueued.
- No Hermes-style in-process cron ticker is introduced.
