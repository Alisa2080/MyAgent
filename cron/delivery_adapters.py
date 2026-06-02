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
    active_dispatch = False

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        return DeliveryResult(False, retryable=False, error="origin delivery waits for origin poll or host bridge pickup")


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
