from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cron.delivery_targets import DeliveryIdentity, DeliveryTarget, DeliveryTargetError, parse_delivery_targets


@dataclass(frozen=True)
class DeliveryValidation:
    ok: bool
    targets: list[DeliveryTarget]
    error: str | None = None


class DeliveryRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, adapter: Any) -> None:
        self._adapters[str(adapter.key)] = adapter

    def get(self, key: str) -> Any | None:
        return self._adapters.get(str(key))

    def adapter_keys(self) -> list[str]:
        return sorted(self._adapters)

    def validate_targets(self, deliver: str | None, *, origin: DeliveryIdentity | None, job: dict[str, Any]) -> DeliveryValidation:
        stored_targets = job.get("delivery_targets")
        if stored_targets is not None:
            targets = [
                DeliveryTarget(
                    raw=str(item.get("raw") or item.get("target_type") or ""),
                    target_type=str(item["target_type"]),
                    adapter_key=str(item["adapter_key"]),
                    address=item.get("address"),
                    thread_id=item.get("thread_id"),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in stored_targets
            ]
            if not targets:
                return DeliveryValidation(False, [], "delivery target is required")
        else:
            try:
                targets = parse_delivery_targets(deliver, origin=origin)
            except DeliveryTargetError as exc:
                return DeliveryValidation(False, [], str(exc))
        for target in targets:
            adapter = self.get(target.adapter_key)
            if adapter is None:
                return DeliveryValidation(False, targets, f"unsupported delivery target: {target.raw}")
            validation = adapter.validate(target, job)
            if not validation.ok:
                return DeliveryValidation(False, targets, validation.error)
        return DeliveryValidation(True, targets)


def default_delivery_registry(*, webhook_sender=None) -> DeliveryRegistry:
    from cron.delivery_adapters import LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter(sender=webhook_sender))
    return registry
