from __future__ import annotations

from gateway.contracts import InboundEvent, SendResult


class FakeAdapter:
    key = "feishu"

    def __init__(self):
        self.sent = []

    def send_text(self, target, message):
        self.sent.append((target, message))
        return SendResult(ok=True)


def test_gateway_dispatch_records_messages_and_sends_response(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    def fake_runner(event, session, origin):
        assert origin["source_type"] == "gateway"
        assert origin["platform"] == "feishu"
        assert origin["chat_id"] == "oc_123"
        return "agent response"

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=fake_runner)

    result = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="message",
            chat_id="oc_123",
            text="hello",
            timestamp="2026-06-03T00:00:00+00:00",
            raw={},
        )
    )
    assert result.ok is True
    assert adapter.sent[0][0].platform == "feishu"
    assert adapter.sent[0][0].target_id == "oc_123"
    assert adapter.sent[0][1].text == "agent response"
    messages = store.list_messages(result.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]