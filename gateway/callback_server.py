from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from gateway.contracts import InboundEvent, InboundParseResult
from gateway.registry import GatewayRegistry


@dataclass(frozen=True)
class CallbackResponse:
    status_code: int
    body: dict[str, Any]


class CallbackApplication:
    def __init__(
        self,
        *,
        registry: GatewayRegistry,
        dispatch: Callable[[InboundEvent], None],
    ) -> None:
        self.registry = registry
        self.dispatch = dispatch
        self._seen_events: set[tuple[str, str]] = set()

    def handle(self, method: str, path: str, headers: dict[str, str], body_bytes: bytes) -> CallbackResponse:
        if method == "GET" and path == "/health":
            return CallbackResponse(200, {"ok": True, "platforms": self.registry.platform_keys()})
        if method != "POST" or not path.startswith("/callback/"):
            return CallbackResponse(404, {"error": "route not found"})

        platform = path.removeprefix("/callback/").strip("/")
        adapter = self.registry.get(platform)
        if adapter is None:
            return CallbackResponse(404, {"error": f"unsupported platform: {platform}"})
        if not hasattr(adapter, "parse_callback"):
            return CallbackResponse(400, {"error": f"platform does not support callbacks: {platform}"})

        try:
            body = json.loads(body_bytes.decode("utf-8") or "{}")
        except ValueError:
            return CallbackResponse(400, {"error": "callback body must be valid JSON"})
        if not isinstance(body, dict):
            return CallbackResponse(400, {"error": "callback body must be a JSON object"})

        result: InboundParseResult = adapter.parse_callback(headers, body)
        if not result.ok:
            return CallbackResponse(result.status_code, {"error": result.error or "callback rejected"})
        if result.event is not None:
            key = (result.event.platform, result.event.event_id)
            if key not in self._seen_events:
                self._seen_events.add(key)
                self.dispatch(result.event)
        if result.response_body is not None:
            return CallbackResponse(result.status_code, result.response_body)
        return CallbackResponse(result.status_code, {"ok": True})
