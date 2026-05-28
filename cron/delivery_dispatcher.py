from __future__ import annotations

from typing import Any

from cron.delivery_registry import DeliveryRegistry, default_delivery_registry
from cron.state_store import StateStore


class DeliveryDispatcher:
    def __init__(self, *, store: StateStore | None = None, registry: DeliveryRegistry | None = None) -> None:
        self.store = store or StateStore()
        self.registry = registry or default_delivery_registry()

    def dispatch_due(self, *, limit: int = 20, adapter_keys: set[str] | None = None) -> dict[str, int]:
        summary = {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
        dispatch_keys = adapter_keys if adapter_keys is not None else {"local", "webhook"}
        for event in self.store.claim_due_delivery_events(limit=limit, adapter_keys=dispatch_keys):
            summary["claimed"] += 1
            adapter = self.registry.get(str(event["adapter_key"]))
            if adapter is None:
                self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=f"unsupported delivery target: {event['target']}",
                    next_attempt_at=None,
                )
                summary["dead"] += 1
                continue
            result = adapter.deliver(event, None, None)
            if result.delivered:
                self.store.update_delivery_event(event["id"], status="delivered", last_error=None, next_attempt_at=None)
                summary["delivered"] += 1
            elif result.retryable:
                self.store.mark_delivery_failed(event["id"], result.error or "delivery failed")
                summary["failed"] += 1
            else:
                self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=result.error or "delivery target is not deliverable",
                    next_attempt_at=None,
                )
                summary["dead"] += 1
        return summary
