from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class AdapterValidation:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    retryable: bool = False
    error: str | None = None


class DeliveryAdapter(Protocol):
    key: str

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        ...

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        ...


class LocalDeliveryAdapter:
    key = "local"
    active_dispatch = False

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        return DeliveryResult(False, retryable=False, error="local delivery is completed synchronously when output is saved")


class OriginDeliveryAdapter:
    key = "origin"
    active_dispatch = True

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        origin = job.get("origin") or {}
        if origin.get("source_type") == "gateway" and origin.get("platform") and origin.get("chat_id"):
            return AdapterValidation(True)
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        origin = (job or {}).get("origin") or {}
        if not origin:
            origin_json = event.get("origin_json")
            if origin_json:
                origin = json.loads(origin_json) if isinstance(origin_json, str) else origin_json
        if origin.get("source_type") != "gateway":
            return DeliveryResult(False, retryable=False, error="origin delivery waits for origin poll or host bridge pickup")
        platform = str(origin.get("platform") or "")
        chat_id = str(origin.get("chat_id") or "")
        if not platform or not chat_id:
            return DeliveryResult(False, retryable=False, error="gateway origin delivery requires platform and chat_id")

        from gateway.contracts import OutboundMessage, PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        gateway_adapter = default_gateway_registry().get(platform)
        if gateway_adapter is None:
            return DeliveryResult(False, retryable=False, error=f"unsupported gateway origin platform: {platform}")

        target = PlatformMessageTarget(
            platform=platform,
            target_type="chat_id",
            target_id=chat_id,
            thread_id=None,
        )
        message = OutboundMessage(
            text=_shorten_feishu_text(_format_origin_delivery_text(event, job, run), _FEISHU_MAX_TEXT_CHARS),
            metadata={"event_id": event.get("id"), "job_id": (job or {}).get("id")},
        )
        result = gateway_adapter.send_text(target, message)
        return DeliveryResult(result.ok, retryable=result.retryable, error=result.error)



class GatewayOriginDeliveryAdapter:
    key = "gateway_origin"
    active_dispatch = True

    def validate(self, target, job):
        origin = job.get("origin") or {}
        if origin.get("source_type") == "gateway" and origin.get("platform") and origin.get("chat_id"):
            return AdapterValidation(True)
        return AdapterValidation(False, "gateway_origin delivery requires gateway origin with platform and chat_id")

    def deliver(self, event, job, run):
        origin = (job or {}).get("origin") or {}
        if not origin:
            origin_json = event.get("origin_json")
            if origin_json:
                origin = json.loads(origin_json) if isinstance(origin_json, str) else origin_json
        platform = str(origin.get("platform") or "")
        chat_id = str(origin.get("chat_id") or "")
        if not platform or not chat_id:
            return DeliveryResult(False, retryable=False, error="gateway origin delivery requires platform and chat_id")

        from gateway.contracts import OutboundMessage, PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        gateway_adapter = default_gateway_registry().get(platform)
        if gateway_adapter is None:
            return DeliveryResult(False, retryable=False, error=f"unsupported gateway origin platform: {platform}")

        target = PlatformMessageTarget(
            platform=platform,
            target_type="chat_id",
            target_id=chat_id,
            thread_id=None,
        )
        message = OutboundMessage(
            text=_shorten_feishu_text(_format_origin_delivery_text(event, job, run), _FEISHU_MAX_TEXT_CHARS),
            metadata={"event_id": event.get("id"), "job_id": (job or {}).get("id")},
        )
        result = gateway_adapter.send_text(target, message)
        return DeliveryResult(result.ok, retryable=result.retryable, error=result.error)


class WebhookDeliveryAdapter:
    key = "webhook"
    active_dispatch = True

    def __init__(self, sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None) -> None:
        from cron.delivery import default_webhook_sender

        self.sender = sender or default_webhook_sender

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        from cron.delivery import validate_webhook_url

        error = validate_webhook_url(target.address)
        return AdapterValidation(error is None, error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="webhook delivery requires a URL")
        payload = json.loads(event["payload_json"])
        payload["event_id"] = event["id"]
        try:
            status, body = self.sender(str(event["address"]), payload, 10)
        except Exception as exc:
            return DeliveryResult(False, retryable=True, error=str(exc))
        if 200 <= status < 300:
            return DeliveryResult(True)
        if status in {408, 429} or status >= 500:
            return DeliveryResult(False, retryable=True, error=f"HTTP {status}: {body}")
        return DeliveryResult(False, retryable=False, error=f"HTTP {status}: {body}")


_WECOM_MAX_MARKDOWN_CHARS = 3900
_RETRYABLE_WECOM_ERRCODES = {45009}


def validate_wecom_webhook_url(url: str | None) -> str | None:
    if not url:
        return "wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL"
    parsed = urlparse(str(url))
    try:
        parsed.port
    except ValueError:
        return "wecom webhook URL port is invalid"
    if parsed.scheme not in {"http", "https"}:
        return "wecom webhook URL must use http or https"
    if parsed.hostname != "qyapi.weixin.qq.com":
        return "wecom webhook URL host must be qyapi.weixin.qq.com"
    if parsed.path != "/cgi-bin/webhook/send":
        return "wecom webhook URL path must be /cgi-bin/webhook/send"
    if not parse_qs(parsed.query).get("key"):
        return "wecom webhook URL requires key query parameter"
    return None


def _shorten_wecom_text(text: Any, max_chars: int) -> str:
    value = "" if text is None else str(text)
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    marker = "\n\n...(truncated; full output saved locally)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _wecom_status(payload: dict[str, Any], run: dict[str, Any] | None) -> str:
    if payload.get("status"):
        return str(payload["status"])
    if run and run.get("status"):
        return str(run["status"])
    if payload.get("error"):
        return "error"
    return "success"


def _format_wecom_markdown(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event["payload_json"])
    job_name = payload.get("job_name") or (job or {}).get("name") or event.get("job_name") or "(unnamed)"
    job_id = payload.get("job_id") or event.get("job_id") or (job or {}).get("id") or "-"
    run_id = payload.get("run_id") or event.get("run_id") or (run or {}).get("id") or "-"
    status = _wecom_status(payload, run)
    output_path = payload.get("output_path") or event.get("output_path") or (run or {}).get("output_path")
    detail = payload.get("final_response") or payload.get("error") or ""

    header = [
        f"### Cron job update: {job_name}",
        f">status: {status}",
        f">job_id: {job_id}",
    ]
    if run_id:
        header.append(f">run_id: {run_id}")
    if output_path:
        header.append(f">output_path: {output_path}")
    body_limit = _WECOM_MAX_MARKDOWN_CHARS - len("\n".join(header)) - 20
    body = _shorten_wecom_text(detail, body_limit)
    return "\n".join(header + ["", body])


class WeComDeliveryAdapter:
    key = "wecom"
    active_dispatch = True

    def __init__(self, sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None) -> None:
        from cron.delivery import default_webhook_sender

        self.sender = sender or default_webhook_sender

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        error = validate_wecom_webhook_url(target.address)
        return AdapterValidation(error is None, error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        address_error = validate_wecom_webhook_url(event.get("address"))
        if address_error:
            return DeliveryResult(
                False,
                retryable=False,
                error=address_error,
            )
        markdown = _format_wecom_markdown(event, job, run)
        payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
        try:
            status, body = self.sender(str(event["address"]), payload, 10)
        except Exception as exc:
            return DeliveryResult(False, retryable=True, error=str(exc))
        if status in {408, 429} or status >= 500:
            return DeliveryResult(False, retryable=True, error=f"HTTP {status}: {body}")
        if not (200 <= status < 300):
            return DeliveryResult(False, retryable=False, error=f"HTTP {status}: {body}")
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError:
            return DeliveryResult(True)
        if not isinstance(parsed, dict):
            return DeliveryResult(True)
        errcode = parsed.get("errcode", 0)
        if errcode in (0, "0", None):
            return DeliveryResult(True)
        errmsg = str(parsed.get("errmsg") or body or "wecom delivery failed")
        try:
            normalized_errcode = int(errcode)
        except (TypeError, ValueError):
            normalized_errcode = None
        retryable = normalized_errcode in _RETRYABLE_WECOM_ERRCODES
        return DeliveryResult(False, retryable=retryable, error=f"WeCom errcode {errcode}: {errmsg}")


def _format_origin_delivery_text(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event.get("payload_json") or "{}")
    lines = [
        f"Cron job: {payload.get('job_name') or (job or {}).get('name') or (job or {}).get('id') or 'unknown'}",
        f"Job ID: {payload.get('job_id') or (job or {}).get('id') or 'unknown'}",
    ]
    run_id = payload.get("run_id") or (run or {}).get("id")
    if run_id:
        lines.append(f"Run ID: {run_id}")
    lines.append(f"Status: {payload.get('status') or 'unknown'}")
    if payload.get("final_response"):
        lines.extend(["", str(payload["final_response"])])
    if payload.get("error"):
        lines.extend(["", f"Error: {payload['error']}"])
    if payload.get("output_path"):
        lines.extend(["", f"Output: {payload['output_path']}"])
    return "\n".join(lines)

_FEISHU_MAX_TEXT_CHARS = 3900


def _shorten_feishu_text(text: Any, max_chars: int) -> str:
    value = "" if text is None else str(text)
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    marker = "\n\n...(truncated; full output saved locally)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _feishu_status(payload: dict[str, Any], run: dict[str, Any] | None) -> str:
    if payload.get("status"):
        return str(payload["status"])
    if run and run.get("status"):
        return str(run["status"])
    if payload.get("error"):
        return "error"
    return "success"


def _format_feishu_text(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event["payload_json"])
    job_name = payload.get("job_name") or (job or {}).get("name") or event.get("job_name") or "(unnamed)"
    job_id = payload.get("job_id") or event.get("job_id") or (job or {}).get("id") or "-"
    run_id = payload.get("run_id") or event.get("run_id") or (run or {}).get("id") or "-"
    status = _feishu_status(payload, run)
    output_path = payload.get("output_path") or event.get("output_path") or (run or {}).get("output_path")
    detail = payload.get("final_response") or payload.get("error") or ""

    lines = [
        f"Cron job update: {job_name}",
        f"status: {status}",
        f"job_id: {job_id}",
    ]
    if run_id:
        lines.append(f"run_id: {run_id}")
    if output_path:
        lines.append(f"output_path: {output_path}")
    body_limit = _FEISHU_MAX_TEXT_CHARS - len("\n".join(lines)) - 20
    body = _shorten_feishu_text(detail, body_limit)
    return _shorten_feishu_text("\n".join(lines + ["", body]), _FEISHU_MAX_TEXT_CHARS)


class FeishuDeliveryAdapter:
    key = "feishu"
    active_dispatch = True

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "feishu delivery requires explicit target: feishu:<chat_id>")

        from gateway.contracts import PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        adapter = default_gateway_registry().get("feishu")
        if adapter is None:
            return AdapterValidation(False, "feishu gateway adapter is not registered")
        result = adapter.validate_target(
            PlatformMessageTarget(
                platform="feishu",
                target_type="chat_id",
                target_id=str(target.address),
                thread_id=target.thread_id,
            )
        )
        return AdapterValidation(result.ok, result.error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        if not event.get("address"):
            return DeliveryResult(
                False,
                retryable=False,
                error="feishu delivery requires explicit target: feishu:<chat_id>",
            )

        from gateway.contracts import OutboundMessage, PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        adapter = default_gateway_registry().get("feishu")
        if adapter is None:
            return DeliveryResult(False, retryable=False, error="feishu gateway adapter is not registered")
        result = adapter.send_text(
            PlatformMessageTarget(
                platform="feishu",
                target_type="chat_id",
                target_id=str(event["address"]),
                thread_id=event.get("thread_id"),
            ),
            OutboundMessage(text=_format_feishu_text(event, job, run)),
        )
        return DeliveryResult(result.ok, retryable=result.retryable, error=result.error)
