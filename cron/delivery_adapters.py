from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Protocol


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

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        output_path = event.get("output_path")
        if output_path and not Path(str(output_path)).exists():
            return DeliveryResult(False, retryable=False, error=f"local output path does not exist: {output_path}")
        return DeliveryResult(True)


class OriginDeliveryAdapter:
    key = "origin"
    active_dispatch = False

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        origin = json.loads(event["origin_json"]) if event.get("origin_json") else {}
        if origin.get("source_type", "cli") != "cli":
            return DeliveryResult(False, retryable=False, error="origin source is not deliverable without a live adapter")
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="origin delivery requires session_id or thread_id")
        return DeliveryResult(True)


class WebhookDeliveryAdapter:
    key = "webhook"

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
