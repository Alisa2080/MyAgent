from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from gateway.contracts import InboundEvent
from gateway.inbox_store import InboxItem, GatewayInboxStore


class GatewayInboxWorker:
    def __init__(
        self,
        *,
        store: GatewayInboxStore,
        dispatch: Callable[[InboundEvent], Any],
        batch_size: int = 5,
        interval_seconds: float = 1.0,
        retry_delays: list[int] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.batch_size = max(1, int(batch_size))
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.retry_delays = retry_delays
        self.sleeper = sleeper
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _event_from_row(row: InboxItem) -> InboundEvent:
        return InboundEvent(
            platform=row.platform,
            event_id=row.event_id,
            event_type=row.event_type,
            chat_id=row.chat_id,
            text=row.text,
            timestamp=datetime.now(timezone.utc).isoformat(),
            thread_id=row.thread_id,
            sender_id=row.sender_id,
            sender_name=row.sender_name,
            raw=row.raw_json,
        )

    def run_once(self) -> int:
        items = self.store.claim_due(limit=self.batch_size)
        if not items:
            return 0

        for item in items:
            event = self._event_from_row(item)
            try:
                result = self.dispatch(event)
                if getattr(result, "ok", True) is False:
                    raise RuntimeError(str(getattr(result, "error", None) or "gateway dispatch failed"))
                self.store.complete(item.id)
            except Exception as exc:
                self.store.fail(
                    item.id,
                    str(exc),
                    retry_delays=self.retry_delays,
                )

        return len(items)

    def run_forever(self) -> None:
        while True:
            if self._stop_event.is_set():
                break
            self.store.recover_stale_processing()
            try:
                count = self.run_once()
            except KeyboardInterrupt:
                break
            if self._stop_event.is_set():
                break
            if count == 0:
                self.sleeper(self.interval_seconds)
