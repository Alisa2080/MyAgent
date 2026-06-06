# Feishu WebSocket Gateway Design

Date: 2026-06-03

## Goal

Add a built-in Feishu WebSocket long-connection gateway so a developer without a public HTTP endpoint can receive Feishu events locally, dispatch them through the existing gateway agent flow, and preserve cron `origin` delivery back to the original Feishu thread.

The first version supports foreground and background operation on WSL2 Ubuntu and macOS. Native Windows services and Windows Task Scheduler are out of scope.

## Current Context

The project already has the core gateway path:

- `GatewayService` and `GatewayDispatcher` dispatch inbound events to the agent.
- `FeishuPlatformAdapter` can parse HTTP callback payloads and send replies through Feishu APIs.
- `GatewaySessionStore` persists gateway sessions and dispatch-level event dedupe.
- Cron delivery supports gateway `origin` delivery through `OriginDeliveryAdapter`.
- `gateway serve` currently runs an HTTP callback server only.

The missing piece is a Feishu WebSocket transport. reference implementation/cron reference logic is most useful for long-lived service behavior: status files, heartbeats, service env, foreground service loops, and systemd/launchd service management.

## Non-Goals

- No native Windows Service or Task Scheduler support in the first version.
- No simultaneous HTTP callback and Feishu WebSocket transport in one gateway process.
- No non-text Feishu message handling beyond ignore/diagnostic accounting.
- No encryption support changes for HTTP callback.
- No merging gateway lifecycle into the cron scheduler service.

## Architecture

Gateway becomes a single-transport long-lived service. HTTP callback and Feishu WebSocket are mutually exclusive gateway processes that share the same business dispatch path, status file, session store, and transport lock semantics.

Commands:

```bash
agent gateway serve --host 0.0.0.0 --port 8765
agent gateway feishu-ws
agent gateway status
agent gateway service install --transport feishu-ws --force
agent gateway service start
agent gateway service stop
agent gateway service restart
agent gateway service status
agent gateway service logs --lines 100
agent gateway service uninstall
agent gateway service env set FEISHU_APP_ID xxx
agent gateway service env set FEISHU_APP_SECRET xxx
agent gateway service env list
agent gateway service env unset FEISHU_APP_SECRET
```

The first implementation should keep `gateway serve` behavior stable. Feishu WebSocket uses a new async inbox path; HTTP callback can remain synchronous initially to reduce risk.

Proposed modules:

- `gateway/inbox_store.py`: SQLite-backed inbound queue.
- `gateway/inbox_worker.py`: claim, dispatch, retry, and complete inbox events.
- `gateway/transports/feishu_ws.py`: Feishu WebSocket SDK integration and event normalization.
- `gateway/service_manager.py`: gateway service commands and platform detection.
- `gateway/service_env.py`: gateway-specific service env file.
- `gateway/service_context.py`: project root, Python executable, and service env context.
- `gateway/service_platforms/systemd_user.py`: WSL2 Ubuntu user service.
- `gateway/service_platforms/launchd_user.py`: macOS launchd user agent.

## Mutual Exclusion

Only one fresh gateway transport may run at a time.

Status records include:

- `process_state`
- `transport`
- `platforms`
- `pid`
- `hostname`
- `started_at`
- `updated_at`
- optional inbox summary

Starting `gateway feishu-ws` fails if a fresh `http` gateway is running. Starting `gateway serve` fails if a fresh `feishu-ws` gateway is running. Stale status may be overwritten after the configured freshness window.

## Inbox Data Model

Use the existing gateway SQLite database under:

```text
<cli_home>/gateway/gateway.sqlite
```

Add table `gateway_inbox_events`:

- `id TEXT PRIMARY KEY`
- `platform TEXT NOT NULL`
- `event_id TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `chat_id TEXT NOT NULL`
- `thread_id TEXT`
- `sender_id TEXT`
- `sender_name TEXT`
- `text TEXT NOT NULL`
- `raw_json TEXT NOT NULL`
- `status TEXT NOT NULL`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `claimed_at TEXT`
- `next_attempt_at TEXT`
- `last_error TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Unique constraint:

```sql
UNIQUE(platform, event_id)
```

Statuses:

- `pending`
- `processing`
- `succeeded`
- `failed`
- `dead`

The inbox is transport-level durability. Existing dispatch-level dedupe in `gateway_inbound_events` remains in place and continues to protect against duplicate agent turns.

## Feishu WebSocket Transport

`gateway/transports/feishu_ws.py` uses `lark-oapi`.

Required environment:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

`FEISHU_CALLBACK_TOKEN` is not required for long-connection mode. It remains relevant only for HTTP callback.

Behavior:

- Register only `im.message.receive_v1`.
- Accept only `message_type == "text"`.
- Extract text from Feishu message content JSON.
- Set `thread_id = message.thread_id or message.message_id`.
- Require `event_id` and `chat_id`; malformed events are ignored with diagnostics.
- Enqueue normalized `InboundEvent` into the inbox and return quickly.
- Do not call the agent from the SDK handler.
- Rely on `lark-oapi` for WebSocket connection and reconnection behavior.

If `lark-oapi` is missing, `agent gateway feishu-ws` returns a clear actionable error telling the user to install it in the active Python environment.

## Inbox Worker

`gateway feishu-ws` starts an inbox worker thread alongside the WebSocket client.

Worker behavior:

- Recover stale `processing` events on startup.
- Claim due events in small batches, default 5.
- Call `GatewayService.handle_event()` for each claimed event.
- Mark success as `succeeded`.
- Treat dispatch duplicate as success.
- On failure, increment attempts, set `last_error`, and schedule retry.
- Mark as `dead` after max attempts.
- On shutdown, stop claiming new work and let the current event finish.

Default retry policy:

- max attempts: 3
- delays: 10s, 60s, 300s

Environment controls:

- `AGENT_GATEWAY_INBOX_MAX_ATTEMPTS`
- `AGENT_GATEWAY_INBOX_WORKER_INTERVAL`
- `AGENT_GATEWAY_INBOX_STALE_SECONDS`

## Background Service Management

Gateway background service is separate from cron background service.

Supported platforms:

- WSL2 Ubuntu with `systemd --user`
- macOS with launchd user agents

Unsupported:

- Native Windows
- WSL2 without systemd enabled

WSL2/systemd-user:

- Unit name: `agent-gateway.service`
- `ExecStart=<python> -m agent_cli.main gateway feishu-ws`
- `WorkingDirectory=<project_root>`
- `PYTHONPATH=<project_root>`
- Read gateway service env file.

macOS/launchd:

- Plist label: `com.agent.gateway`
- Path: `~/Library/LaunchAgents/com.agent.gateway.plist`
- Program arguments point to the active Python executable and `-m agent_cli.main gateway feishu-ws`.
- Working directory is the project root.
- Environment includes `PYTHONPATH` and gateway service env values.

Gateway service env is separate from cron service env:

```text
<cli_home>/gateway/service.env
```

Changing env values should tell users to restart the gateway service. On macOS, if env values are embedded in the plist, users may need `gateway service install --force` after env changes.

## Status and Diagnostics

`agent gateway status` should show:

- whether gateway is running
- transport
- platforms
- updated timestamp
- inbox counts by status
- last inbox error, if any

Optional follow-up commands:

- `agent gateway inbox --limit 20`
- `agent gateway retry-inbox <event_id>`

These are useful but not required for the first implementation if `status` exposes enough queue health.

## Error Handling

- Missing Feishu env fails startup before opening the WebSocket.
- Missing `lark-oapi` fails startup with installation guidance.
- Unsupported platform for service install/status returns a clear error.
- WSL2 without systemd returns a clear error and suggests foreground mode.
- Malformed or unsupported Feishu events are ignored with counters/logs, not added to the inbox.
- Worker failures do not block new event ingestion.
- Stale `processing` events are recovered on startup.

## Testing Strategy

Unit tests:

- Feishu WebSocket payload normalization creates the expected `InboundEvent`.
- Non-text messages are ignored.
- Missing `chat_id` or `event_id` is ignored with diagnostic information.
- `thread_id` prefers Feishu `thread_id`, then `message_id`.
- Missing `lark-oapi` produces a clear error.

Inbox/worker tests:

- Duplicate `(platform, event_id)` enqueue is idempotent.
- Successful dispatch marks an event `succeeded`.
- Dispatch failure marks `failed` and schedules retry.
- Max attempts marks `dead`.
- Stale `processing` is recovered.
- Dispatch duplicate is treated as successful inbox completion.
- Origin identity preserves the original Feishu thread.

CLI/service tests:

- `gateway feishu-ws` validates required env.
- `gateway status` includes transport and inbox stats.
- `gateway serve` and `gateway feishu-ws` are mutually exclusive.
- `gateway service env set/list/unset` works.
- systemd unit includes Python, `PYTHONPATH`, project root, env file, and `feishu-ws` transport.
- launchd plist includes Python, `PYTHONPATH`, project root, service env, and `feishu-ws` transport.
- Unsupported platforms return explicit errors.

Full project pytest must pass.

## Manual Acceptance

1. Configure Feishu event subscription to use long-connection mode.
2. Start `agent gateway feishu-ws` on WSL2 Ubuntu or macOS.
3. Send a text message to the Feishu bot.
4. Confirm the bot replies through the existing gateway dispatch path.
5. Ask the bot to create a one-shot cron job with `deliver=origin`.
6. Run `agent cron serve` or the cron service.
7. Confirm cron output returns to the original Feishu thread.
8. Stop foreground gateway.
9. Install and start `agent gateway service --transport feishu-ws`.
10. Repeat the message and cron-origin flow.

Success means:

- No public HTTP endpoint is needed.
- Feishu text messages trigger the agent.
- Cron `origin` delivery returns to the original Feishu thread.
- Unprocessed inbox events survive gateway process restart.
- Status/service/env/log commands are enough for routine diagnosis.
