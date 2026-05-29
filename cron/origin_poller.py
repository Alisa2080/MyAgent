"""Origin polling for external delivery targets."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cron.delivery_store import DeliveryStore

logger = logging.getLogger(__name__)


class DeliveryPoller:
    """Protocol for origin pollers that can be registered with the delivery system."""

    key: str

    def validate(self, target: Any, job: dict[str, Any]) -> Any:
        """Validate the target for this poller."""
        ...

    def poll(
        self,
        target: Any,
        job: dict[str, Any],
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Poll for new events.

        Args:
            target: The delivery target to poll.
            job: The job configuration.
            cursor: Pagination cursor from previous poll (None for first poll).
            limit: Maximum number of events to return.

        Returns:
            dict with keys:
                - events: list of event dicts
                - next_cursor: cursor for next page, or None if done
        """
        ...


def default_delivery_store() -> "DeliveryStore":
    from cron.delivery_store import DeliveryStore

    return DeliveryStore()


def poll_deliveries(
    pollers: list[DeliveryPoller],
    store: "DeliveryStore | None" = None,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Poll all registered pollers for new delivery events.

    Args:
        pollers: List of poller instances to poll.
        store: Optional DeliveryStore instance. If not provided, uses default.
        limit: Maximum events per poller.

    Returns:
        dict with:
            - polled: number of pollers that were queried
            - collected: total events collected across all pollers
    """
    if store is None:
        store = default_delivery_store()

    total_polled = 0
    total_collected = 0

    for poller in pollers:
        try:
            poll_state = store.get_poll_state(poller.key)
            cursor = poll_state["cursor"] if poll_state else None

            result = poller.poll(
                store.target_for_adapter(poller.key),
                store.job_for_adapter(poller.key),
                cursor=cursor,
                limit=limit,
            )

            events = result.get("events") or []
            for event in events:
                store.enqueue_event(poller.key, event, origin=cursor)

            next_cursor = result.get("next_cursor")
            store.save_poll_state(poller.key, next_cursor)

            total_polled += 1
            total_collected += len(events)
        except Exception as e:
            logger.error("Poller %s failed: %s", poller.key, e)
            continue

    return {"polled": total_polled, "collected": total_collected}
