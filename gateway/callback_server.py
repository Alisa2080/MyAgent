from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            if key in self._seen_events:
                if result.response_body is not None:
                    return CallbackResponse(result.status_code, result.response_body)
                return CallbackResponse(result.status_code, {"ok": True})
            try:
                dispatch_result = self.dispatch(result.event)
            except Exception as exc:
                return CallbackResponse(500, {"error": str(exc)})
            if _dispatch_failed(dispatch_result):
                return CallbackResponse(500, {"error": _dispatch_error(dispatch_result)})
            self._seen_events.add(key)
        if result.response_body is not None:
            return CallbackResponse(result.status_code, result.response_body)
        return CallbackResponse(result.status_code, {"ok": True})


def make_callback_handler(app: CallbackApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _write_response(self, response: CallbackResponse) -> None:
            body = json.dumps(response.body).encode("utf-8")
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._write_response(app.handle("GET", self.path, dict(self.headers), b""))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b""
            self._write_response(app.handle("POST", self.path, dict(self.headers), body))

        def log_message(self, format: str, *args: Any) -> None:
            return None

    return Handler


def serve_callback_http(app: CallbackApplication, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), make_callback_handler(app))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _dispatch_failed(result: Any) -> bool:
    return bool(getattr(result, "ok", True) is False)


def _dispatch_error(result: Any) -> str:
    return str(getattr(result, "error", None) or "gateway dispatch failed")
