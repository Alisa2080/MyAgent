from __future__ import annotations

from pathlib import Path
from typing import Callable

from gateway.contracts import InboundEvent
from gateway.dispatch import GatewayDispatcher
from gateway.registry import GatewayRegistry, default_gateway_registry
from gateway.session_store import GatewaySessionStore
from gateway.service_state import write_gateway_status


class GatewayService:
    def __init__(
        self,
        *,
        home: str | Path,
        registry: GatewayRegistry | None = None,
        dispatch: Callable[[InboundEvent], object] | None = None,
    ) -> None:
        self.home = Path(home)
        self.registry = registry or default_gateway_registry()
        if dispatch is None:
            store = GatewaySessionStore(self.home / "gateway" / "gateway.sqlite")
            dispatcher = GatewayDispatcher(store=store, registry=self.registry)
            self.dispatch = dispatcher.dispatch
        else:
            self.dispatch = dispatch

    def handle_event(self, event: InboundEvent) -> None:
        result = self.dispatch(event)
        if getattr(result, "ok", True) is False:
            raise RuntimeError(str(getattr(result, "error", None) or "gateway dispatch failed"))

    def write_status(self, *, process_state: str, transport: str = "http") -> None:
        write_gateway_status(
            self.home,
            {
                "process_state": process_state,
                "transport": transport,
                "platforms": self.registry.platform_keys(),
            },
        )
