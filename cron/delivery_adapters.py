from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterValidation:
    ok: bool
    error: str | None = None


class LocalDeliveryAdapter:
    key = "local"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)


class OriginDeliveryAdapter:
    key = "origin"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)


class WebhookDeliveryAdapter:
    key = "webhook"

    def validate(self, target, job: dict[str, Any]) -> AdapterValidation:
        from cron.delivery import validate_webhook_url

        error = validate_webhook_url(target.address)
        return AdapterValidation(error is None, error)
