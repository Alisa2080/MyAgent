# Feishu Gateway Cron E2E and Orchestration Design

Date: 2026-06-04

## Summary

Add the remaining Feishu gateway cron integration work needed for the Hermes-style experience. The system should have one true end-to-end acceptance test from Feishu inbound event through cron job creation, cron service execution, origin delivery, and Feishu outbound send. Cron reports created from a gateway origin should default to a new message in the original chat, not a reply to the original message. Gateway service installation should optionally install the cron service with `--with-cron`, while runtime startup continues to warn rather than auto-start cron.

## Goals

- Prove the complete Feishu gateway cron path with one acceptance test:
  `Feishu inbound -> gateway agent runtime origin -> cronjob create -> cron service tick -> OriginDeliveryAdapter -> Feishu adapter send`.
- Change gateway-origin cron report delivery to default to `chat_id` as a new Feishu message.
- Preserve origin `thread_id` for audit/debugging, but do not use it for default cron report delivery.
- Keep explicit delivery target semantics intact for targets that intentionally include a thread id.
- Add installation-time service orchestration with `gateway service install --with-cron`.
- Preserve runtime behavior: `gateway feishu-ws` warns when cron is not running but does not start or install cron.

## Non-Goals

- Do not make `gateway service start` start cron service.
- Do not add an embedded cron ticker inside the gateway process.
- Do not call real Feishu APIs, real LLMs, or real system service managers in E2E tests.
- Do not introduce a new delivery syntax for forcing reply/thread behavior in this scope.
- Do not remove `thread_id` from stored gateway origin metadata.

## End-to-End Acceptance Test

Add a focused E2E test, preferably in `tests/test_gateway_feishu_ws_e2e.py` or a new `tests/test_feishu_gateway_cron_e2e.py`.

The test should exercise real local components and fake only external boundaries:

1. Build a Feishu WS-style payload for `im.message.receive_v1`.
2. Normalize it with `normalize_feishu_ws_event()`.
3. Enqueue it in `GatewayInboxStore`.
4. Process it with `GatewayInboxWorker` and `GatewayService`.
5. The gateway dispatch path should call a runner that uses `run_cronjob_action(action="create", runtime=fake_runtime, deliver="origin", ...)` so the real cronjob tool creates the job from gateway runtime origin.
6. Force the created cron job due.
7. Run cron once with `CronService(..., once=True)` or `cron.scheduler.tick()` using a fake job runner that returns a successful report.
8. Let `enqueue_result()` and `process_due()` dispatch the origin delivery through a fake Feishu gateway adapter.

Required assertions:

- The created job has `deliver == "origin"`.
- `job.origin` includes:
  - `source_type == "gateway"`
  - `platform == "feishu"`
  - original `chat_id`
  - original `thread_id`
  - gateway `session_id`
- A delivery event is created with `adapter_key == "origin"` and reaches `status == "delivered"`.
- The final Feishu adapter send target has:
  - `platform == "feishu"`
  - `target_type == "chat_id"`
  - `target_id == original chat_id`
  - `thread_id is None`
- The final outbound text includes the cron report content.

## Feishu Cron Report Send Strategy

Default gateway-origin cron reports should be sent as new messages to the original Feishu chat.

Implementation expectation:

- In `OriginDeliveryAdapter`, when `origin.source_type == "gateway"`, construct `PlatformMessageTarget(..., thread_id=None)` for default `deliver="origin"` delivery.
- Apply the same default to `GatewayOriginDeliveryAdapter` if it remains available, so both origin-delivery implementations agree.
- Keep `origin["thread_id"]` unchanged in stored job metadata and delivery event metadata.
- Existing direct/explicit Feishu targets such as `feishu:<chat_id>:<thread_id>` should continue to pass the explicit `thread_id` through their delivery path, including the final `PlatformMessageTarget.thread_id`.
- Existing gateway interactive replies may continue to use inbound event `thread_id`; this change is only for cron origin delivery.

This behavior avoids making scheduled reports appear as replies to the original cron-creation message. It matches the expected chat experience: a user can keep talking to the bot, and scheduled reports arrive as normal messages in the same chat.

## Installation-Time Service Orchestration

Add `--with-cron` to:

```bash
python3 -m agent_cli.main gateway service install --with-cron
```

Behavior:

- First install the gateway service using existing gateway service install behavior.
- If gateway service install fails, return that failure and do not call cron service install.
- If gateway service install succeeds, call `cron.service_manager.install_service(...)`.
- Use cron service defaults:
  - `interval_seconds=60`
  - `lease_seconds=180`
  - `force=args.force`
- If cron install succeeds, return success with a combined message containing both gateway and cron install results.
- If cron install fails, return non-zero because the user explicitly requested `--with-cron`.
- Do not start either service.
- Do not change `gateway service start`, `restart`, `stop`, `status`, or foreground `gateway feishu-ws` behavior.

## Runtime Warning

Keep the existing `gateway feishu-ws` cron warning behavior:

- If cron is unhealthy, print a clear WARN with the command to start cron.
- If managed cron service is unsupported, point to `python3 -m agent_cli.main cron serve`.
- Continue starting the gateway unless the gateway itself fails.

This remains a runtime safety net for users who start the foreground gateway directly.

## Error Handling

- E2E tests should fail closed if cron job creation does not capture gateway origin.
- E2E tests should fail if delivery is only queued but not dispatched.
- Cron-origin delivery should not discard origin `thread_id`; it should only suppress it in the default Feishu send target.
- `gateway service install --with-cron` should report cron install errors explicitly after gateway install succeeds.
- Gateway install failure should not mask itself behind cron install attempts.

## Testing

Add or update tests for:

- Full Feishu gateway cron E2E acceptance path.
- `OriginDeliveryAdapter` default gateway-origin delivery sends to chat with `thread_id is None`.
- Existing explicit Feishu target delivery keeps explicit `thread_id`.
- `gateway service install --with-cron` parser support.
- Handler behavior:
  - gateway install succeeds, cron install succeeds -> exit 0 and combined message.
  - gateway install fails -> cron install is not called and exit code is the gateway failure.
  - cron install fails -> exit code is non-zero and message includes cron failure.
- Existing gateway/cron delivery tests continue to pass.

## Acceptance Criteria

- The test suite contains one complete E2E test covering Feishu inbound through cron-origin Feishu outbound send.
- Gateway-origin cron report delivery defaults to a new message in the original Feishu chat.
- Explicit thread delivery remains available for explicit targets.
- `gateway service install --with-cron` installs gateway service and then cron service, without starting either service.
- Foreground `gateway feishu-ws` continues to warn but not auto-start cron.
