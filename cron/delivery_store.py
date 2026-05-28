from __future__ import annotations

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

    def stats(self) -> dict[str, int]:
        return self._store.delivery_stats()

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
