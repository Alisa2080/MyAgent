# WeCom Cron Delivery Design

## Goal

Add first-class Enterprise WeChat group robot delivery for cron jobs while staying inside the current cron runtime and delivery architecture.

The first release supports Enterprise WeChat group robot webhooks only. It does not add personal WeChat delivery, Enterprise WeChat application messages, live Hermes gateway routing, mentions, media upload, or multi-message splitting.

## Context

The current cron system already has the runtime pieces needed for a production delivery adapter:

- `CronService` runs periodic ticks under a scheduler leader lease and writes heartbeat/status.
- `scheduler.tick()` performs delivery maintenance at the start of every tick, even when no jobs are due.
- `DeliveryDispatcher` claims due delivery events, invokes registered adapters, and syncs run/job delivery state.
- `DeliveryRegistry` validates delivery targets and supports default adapters plus registered adapter factories.
- `WebhookDeliveryAdapter` already establishes the retry classification pattern for HTTP delivery.

Hermes treats platforms such as `wecom` and `weixin` as platform delivery targets and routes them through a gateway/platform adapter layer. This project should not copy that whole gateway layer for the first WeCom milestone. The current project is better served by a native `wecom` delivery adapter that plugs into the existing delivery event, retry, and observability model.

## Scope

In scope:

- Add a default `wecom` delivery adapter.
- Support `deliver="wecom"` using `AGENT_CRON_WECOM_WEBHOOK_URL`.
- Support `deliver="wecom:<webhook_url>"` using the explicit webhook URL.
- Send Enterprise WeChat group robot markdown payloads.
- Truncate long message content and include the saved local `output_path`.
- Reuse the current delivery retry/dead-letter state machine.
- Add doctor validation for active WeCom delivery jobs.
- Add unit and E2E coverage for successful delivery, retry, dead-letter, env/default target resolution, and service tick integration.

Out of scope:

- Personal WeChat delivery.
- Enterprise WeChat application messages using `corp_id`, `corp_secret`, or `agent_id`.
- `mentioned_list` and `mentioned_mobile_list`.
- Attachment/media forwarding.
- Automatic splitting into multiple messages.
- Hermes live gateway adapter routing.

## Delivery Target Semantics

`cron.delivery_targets` should treat WeCom as a native platform target:

- `wecom` resolves to adapter key `wecom` and address `AGENT_CRON_WECOM_WEBHOOK_URL`.
- `wecom:<url>` resolves to adapter key `wecom` and address `<url>`.

`deliver="wecom"` must fail validation when the environment variable is missing or blank. This should fail at job create/update validation rather than creating a job that later dead-letters every run.

The resolved target address should be stored in `delivery_targets` at create/update time, matching the existing stored-target behavior used by webhook delivery. If the environment variable changes later, existing jobs continue using their stored address until updated.

## Adapter Design

Add `WeComDeliveryAdapter` alongside the existing local, origin, and webhook adapters.

Responsibilities:

- `key = "wecom"`.
- `active_dispatch = True`.
- Validate the target address.
- Format a markdown message from the delivery event payload, job, and run.
- POST an Enterprise WeChat group robot payload.
- Convert HTTP and WeCom business responses into `DeliveryResult`.

The default registry should register the adapter in `build_delivery_registry()`, so `wecom` appears in default validation, dispatch, status, and doctor paths.

## URL Validation

Production validation should accept only HTTP(S) URLs and should be biased toward real Enterprise WeChat robot URLs:

- Scheme must be `https` or `http`.
- Host should be `qyapi.weixin.qq.com`.
- Path should include `/cgi-bin/webhook/send`.
- A `key` query parameter should be present.

Tests can inject a sender and use deterministic fake responses without making real network calls.

If later local integration testing needs non-WeCom hosts, that should be added explicitly as a test-only sender path or a documented development override, not silently accepted by production validation.

## Message Format

The adapter sends markdown:

```json
{
  "msgtype": "markdown",
  "markdown": {
    "content": "..."
  }
}
```

The markdown content should include:

- Cron job name.
- Job ID.
- Run ID when available.
- Status, using success/failure from the event payload or run.
- Final response for successful runs.
- Error for failed runs.
- Output path when available.

The full cron output remains in the current saved output file. The WeCom message is a readable notification, not a replacement for stored output.

Long content should be truncated before sending. The truncation text must preserve the output path when one is available so users can inspect the full result locally.

## Failure Handling

The adapter should follow the current webhook delivery retry style:

- Network exception: retryable.
- HTTP 2xx: inspect body when possible.
- HTTP 408, 429, or 5xx: retryable.
- Other HTTP 4xx: not retryable, dead-letter.

Enterprise WeChat group robot responses normally include JSON with `errcode` and `errmsg`.

- `errcode == 0`: delivered.
- temporary or rate-limit style errors: retryable.
- other non-zero `errcode`: not retryable, dead-letter with `errmsg`.

If the HTTP status is 2xx and the body is not valid JSON, treat it as delivered for the first release. This keeps the adapter tolerant of proxies and test senders while preserving strict handling for explicit WeCom error JSON.

## Observability And Doctor

No new scheduler status model is needed. WeCom events should flow through the existing delivery summary:

- `claimed`
- `delivered`
- `failed`
- `dead`

`agent cron doctor` should:

- List `wecom` in `delivery adapters`.
- Validate active WeCom jobs.
- Fail when an active job uses `deliver="wecom"` and no default webhook URL is configured.
- Fail when a WeCom webhook URL is malformed.

`agent cron status` should not add WeCom-specific fields in the first release.

## Testing

Unit tests:

- `deliver="wecom"` resolves to `AGENT_CRON_WECOM_WEBHOOK_URL`.
- `deliver="wecom:<url>"` stores the explicit URL and overrides the environment variable.
- Missing default URL fails validation for `deliver="wecom"`.
- Invalid WeCom URL fails validation.
- Adapter sends markdown payload with job, run, status, final response/error, and output path.
- HTTP 200 plus `{"errcode":0}` marks delivered.
- HTTP 429 or 5xx marks retryable failed.
- HTTP 400 marks dead.
- HTTP 200 plus non-zero non-temporary `errcode` marks dead.
- Non-JSON HTTP 2xx is treated as delivered.

E2E tests:

- Create a due job with `deliver="wecom"`, run `CronService` once with a fake sender, verify run success, output saved, delivery event delivered, and payload shape.
- First tick returns a retryable WeCom failure; a later tick with no due jobs retries and delivers the pending event.
- Doctor reports `wecom` as a known adapter and fails an active `deliver="wecom"` job when the default webhook URL is missing.

## Acceptance Criteria

- A user can create a cron job with `deliver="wecom"` when `AGENT_CRON_WECOM_WEBHOOK_URL` is configured.
- A user can create a cron job with `deliver="wecom:<enterprise_wechat_robot_url>"`.
- The service delivers cron results to Enterprise WeChat group robot webhooks without changing scheduler flow.
- Failed WeCom delivery is recoverable through the existing delivery retry path.
- Permanent WeCom delivery errors become dead-letter events with clear errors.
- Doctor can detect missing or malformed WeCom delivery configuration before runtime.
