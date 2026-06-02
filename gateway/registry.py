from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from gateway.contracts import PlatformAdapter


GatewayAdapterFactory = Callable[..., PlatformAdapter | None]
_ADAPTER_FACTORIES: list[GatewayAdapterFactory] = []


def register_gateway_adapter_factory(factory: GatewayAdapterFactory) -> None:
    _ADAPTER_FACTORIES.append(factory)


def clear_gateway_adapter_factories() -> None:
    _ADAPTER_FACTORIES.clear()


class GatewayRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        self._adapters[str(adapter.key)] = adapter

    def get(self, platform: str) -> PlatformAdapter | None:
        return self._adapters.get(str(platform))

    def platform_keys(self) -> list[str]:
        return sorted(self._adapters)


def build_gateway_registry(
    *, http_sender: Any = None, extra_adapters: Iterable[PlatformAdapter] | None = None
) -> GatewayRegistry:
    from gateway.platforms.feishu import FeishuPlatformAdapter

    registry = GatewayRegistry()
    registry.register(FeishuPlatformAdapter(http_sender=http_sender))
    for adapter in extra_adapters or []:
        registry.register(adapter)
    for factory in list(_ADAPTER_FACTORIES):
        adapter = factory(http_sender=http_sender)
        if adapter is not None:
            registry.register(adapter)
    return registry


def default_gateway_registry(*, http_sender: Any = None) -> GatewayRegistry:
    return build_gateway_registry(http_sender=http_sender)
