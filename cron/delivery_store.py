from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cron.state_store import RETRY_DELAYS, StateStore, get_state_db_path, utc_now

STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}


def get_delivery_db_path() -> Path:
    return get_state_db_path()


class DeliveryStore:
    def __init__(self, path: Path | None = None, *, max_attempts: int = 5) -> None:
        self._store = StateStore(path, max_delivery_attempts=max_attempts)
        self.path = self._store.path
        self.max_attempts = max_attempts
        self._init_poll_state_schema()

    def _init_poll_state_schema(self) -> None:
        """Initialize the poll_state table if it doesn't exist."""
        self._store._init_poll_state_schema()

    def enqueue_event(self, adapter_key: str, event: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """Enqueue a polled event into the delivery system.

        Args:
            adapter_key: The adapter that produced this event.
            event: The event data.
            origin: The cursor/pagination token when this event was polled.

        Returns:
            The enqueued delivery event.
        """
        import uuid

        event_id = uuid.uuid4().hex
        now_text = utc_now().isoformat()

        return self._store.enqueue_delivery_event(
            job_id=None,
            run_id=None,
            job_name=None,
            run_at=now_text,
            target=adapter_key,
            target_type="origin",
            adapter_key=adapter_key,
            address=None,
            thread_id=None,
            origin={"cursor": origin, "event": event} if origin else {"event": event},
            final_response=None,
            output_path=None,
            payload=event,
            status="pending",
        )

    def target_for_adapter(self, adapter_key: str) -> Any:
        """Get the delivery target for an adapter.

        This returns None as poller targets are typically determined
        from the adapter's configuration rather than from stored targets.
        """
        return None

    def job_for_adapter(self, adapter_key: str) -> dict[str, Any]:
        """Get a dummy job dict for an adapter.

        Poll adapters typically don't need job configuration,
        so we return an empty dict.
        """
        return {}

    def get_poll_state(self, adapter_key: str) -> dict[str, Any] | None:
        """Retrieve the persisted poll state for an adapter.

        Args:
            adapter_key: The adapter key to look up.

        Returns:
            dict with adapter_key, cursor, updated_at, or None if not found.
        """
        return self._store.get_poll_state(adapter_key)

    def save_poll_state(self, adapter_key: str, cursor: str | None) -> None:
        """Persist the poll state for an adapter.

        Args:
            adapter_key: The adapter key.
            cursor: The next cursor value to save, or None if no more pages.
        """
        self._store.save_poll_state(adapter_key, cursor)

    def register_delivery_adapter(self, adapter: Any) -> None:
        """Register a delivery adapter with the default registry.

        Args:
            adapter: The adapter to register.
        """
        from cron.delivery_registry import default_delivery_registry

        registry = default_delivery_registry()
        registry.register(adapter)

    def enqueue(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.enqueue_delivery_event(
            run_id=kwargs.pop("run_id", None),
            adapter_key=kwargs.pop("adapter_key", kwargs.get("target_type")),
            address=kwargs.pop("address", kwargs.pop("target_id", None)),
            thread_id=kwargs.pop("thread_id", None),
            origin=kwargs.pop("origin", None),
            **kwargs,
        )

    def get(self, event_id: str) -> dict[str, Any]:
        return self._store.get_delivery_event(event_id)

    def update_event(self, event_id: str, **updates: Any) -> dict[str, Any]:
        return self._store.update_delivery_event(event_id, **updates)

    def claim_due(self, *, limit: int = 20, target_types: list[str] | tuple[str, ...] | set[str] | None = None) -> list[dict[str, Any]]:
        adapter_keys = None
        if target_types is not None:
            adapter_keys = set(str(t) for t in target_types)
        return self._store.claim_due_delivery_events(limit=limit, adapter_keys=adapter_keys)

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        return self.update_event(event_id, status="delivered", last_error=None, next_attempt_at=None)

    def mark_dead(self, event_id: str, error: str) -> dict[str, Any]:
        return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)

    def mark_failed(self, event_id: str, error: str) -> dict[str, Any]:
        return self._store.mark_delivery_failed(event_id, error)

    def delete_events(self, event_ids: list[str]) -> int:
        return self._store.delete_delivery_events(event_ids)

    def stats(self) -> dict[str, int]:
        return self._store.delivery_stats()

    def origin_pending_count(self) -> int:
        return self._store.origin_pending_count()

    def pending_origin_events(self, thread_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._store.pending_origin_events(str(thread_id), limit=limit)

    def recent_errors(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('failed', 'dead') OR last_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stale_delivering(self, *, max_age_seconds: int = 600) -> list[dict[str, Any]]:
        from datetime import timedelta
        cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery_events WHERE status = 'delivering' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._store.list_delivery_events(job_id=job_id, run_id=run_id, limit=limit)

    def retry(self, event_id: str) -> dict[str, Any]:
        event = self.get(event_id)
        if event["status"] not in {"failed", "dead"}:
            raise ValueError(f"delivery event is not retryable: {event['status']}")
        return self.update_event(event_id, status="pending", last_error=None)
