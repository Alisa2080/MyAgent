from __future__ import annotations

from pathlib import Path
from typing import Callable

from gateway.contracts import InboundEvent
from gateway.registry import GatewayRegistry, default_gateway_registry
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
        self.dispatch = dispatch or (lambda event: None)

    def handle_event(self, event: InboundEvent) -> None:
        self.dispatch(event)

    def write_status(self, *, process_state: str) -> None:
        write_gateway_status(
            self.home,
            {
                "process_state": process_state,
                "platforms": self.registry.platform_keys(),
            },
        )