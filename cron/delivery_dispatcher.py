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
        self.store.recover_stale_delivery_events()
        if adapter_keys is None:
            for event in self.store.dead_letter_unsupported_delivery_events(
                supported_adapter_keys=set(self.registry.adapter_keys()),
                ignored_adapter_keys={"origin"},
                limit=limit,
            ):
                self._sync_after_event_update(event)
                summary["dead"] += 1
        for event in self.store.claim_due_delivery_events(limit=limit, adapter_keys=dispatch_keys):
            summary["claimed"] += 1
            adapter = self.registry.get(str(event["adapter_key"]))
            job = self.store.get_job(str(event["job_id"])) if event.get("job_id") else None
            run = None
            if event.get("run_id"):
                try:
                    run = self.store.get_run(str(event["run_id"]))
                except KeyError:
                    run = None
            if adapter is None:
                updated = self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=f"unsupported delivery target: {event['target']}",
                    next_attempt_at=None,
                )
                self._sync_after_event_update(updated)
                summary["dead"] += 1
                continue
            try:
                result = adapter.deliver(event, job, run)
            except Exception as exc:
                updated = self.store.mark_delivery_failed(event["id"], str(exc))
                self._sync_after_event_update(updated)
                summary["dead" if updated["status"] == "dead" else "failed"] += 1
                continue
            if result.delivered:
                updated = self.store.update_delivery_event(event["id"], status="delivered", last_error=None, next_attempt_at=None)
                self._sync_after_event_update(updated)
                summary["delivered"] += 1
            elif result.retryable:
                updated = self.store.mark_delivery_failed(event["id"], result.error or "delivery failed")
                self._sync_after_event_update(updated)
                summary["dead" if updated["status"] == "dead" else "failed"] += 1
            else:
                updated = self.store.update_delivery_event(
                    event["id"],
                    status="dead",
                    last_error=result.error or "delivery target is not deliverable",
                    next_attempt_at=None,
                )
                self._sync_after_event_update(updated)
                summary["dead"] += 1
        return summary

    def _sync_after_event_update(self, event: dict[str, Any]) -> None:
        if event.get("run_id"):
            self.store.update_run_delivery_status(str(event["run_id"]))
        if event.get("job_id") and event.get("status") in {"delivered", "failed", "dead"}:
            error = self.store.delivery_error_for_run(str(event["run_id"])) if event.get("run_id") else None
            if event.get("status") == "dead" and error is None:
                error = str(event.get("last_error") or "delivery failed")
            self.store.update_job_delivery_error(
                str(event["job_id"]),
                error,
            )
