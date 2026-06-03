from __future__ import annotations

import json


class FakeAdapter:
    key = "fake" 

    def parse_callback(self, headers, body):
        from gateway.contracts import InboundEvent, InboundParseResult

        if body.get("challenge"):
            return InboundParseResult(ok=True, response_body={"challenge": body["challenge"]}, status_code=200)
        return InboundParseResult(
            ok=True,
            event=InboundEvent(
                platform="fake",
                event_id=body["event_id"],
                event_type="message",
                chat_id=body["chat_id"],
                text=body["text"],
                timestamp="2026-06-03T00:00:00+00:00",
                raw=body,
            ),
        )


def test_callback_health_returns_platforms():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=lambda event: None)

    response = app.handle("GET", "/health", {}, b"")

    assert response.status_code == 200
    assert response.body["ok"] is True
    assert response.body["platforms"] == ["fake"]


def test_callback_challenge_returns_adapter_body():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=lambda event: None)

    response = app.handle("POST", "/callback/fake", {}, json.dumps({"challenge": "abc"}).encode())

    assert response.status_code == 200
    assert response.body == {"challenge": "abc"}


def test_callback_dispatches_inbound_event_once():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    dispatched = []
    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=dispatched.append)

    body = json.dumps({"event_id": "evt-1", "chat_id": "chat-1", "text": "hello"}).encode()
    first = app.handle("POST", "/callback/fake", {}, body)
    second = app.handle("POST", "/callback/fake", {}, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert [event.event_id for event in dispatched] == ["evt-1"]


def test_callback_unknown_platform_returns_404():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    app = CallbackApplication(registry=GatewayRegistry(), dispatch=lambda event: None)

    response = app.handle("POST", "/callback/missing", {}, b"{}")

    assert response.status_code == 404
    assert "unsupported platform" in response.body["error"]
