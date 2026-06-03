from __future__ import annotations


def test_gateway_contracts_are_plain_values():
    from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult

    target = PlatformMessageTarget(
        platform="feishu",
        target_type="chat_id",
        target_id="oc_123",
        thread_id="thread-1",
    )
    message = OutboundMessage(text="hello", metadata={"job_id": "job-1"})
    result = SendResult(ok=True)

    assert target.platform == "feishu"
    assert target.target_type == "chat_id"
    assert target.target_id == "oc_123"
    assert target.thread_id == "thread-1"
    assert message.text == "hello"
    assert message.metadata == {"job_id": "job-1"}
    assert result.ok is True
    assert result.error is None
    assert result.retryable is False


def test_gateway_registry_registers_and_returns_adapter():
    from gateway.contracts import SendResult
    from gateway.registry import GatewayRegistry

    class FakeAdapter:
        key = "fake"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

    registry = GatewayRegistry()
    adapter = FakeAdapter()
    registry.register(adapter)

    assert registry.get("fake") is adapter
    assert registry.get("missing") is None
    assert registry.platform_keys() == ["fake"]


def test_default_gateway_registry_includes_feishu():
    from gateway.registry import default_gateway_registry

    registry = default_gateway_registry()

    assert "feishu" in registry.platform_keys()
    assert registry.get("feishu") is not None


def test_gateway_registry_factory_override_is_isolated():
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        registry = gateway_registry.default_gateway_registry()
        assert isinstance(registry.get("feishu"), FakeFeishuAdapter)
    finally:
        gateway_registry.clear_gateway_adapter_factories()


def test_gateway_inbound_contracts_are_plain_values():
    from gateway.contracts import InboundEvent, InboundParseResult

    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        thread_id="thread-1",
        sender_id="ou_123",
        sender_name="Miku",
        raw={"event": {"message": {"text": "hello"}}},
    )
    result = InboundParseResult(ok=True, event=event, status_code=200, response_body={"ok": True})

    assert event.platform == "feishu"
    assert event.event_id == "evt-1"
    assert event.chat_id == "oc_123"
    assert event.thread_id == "thread-1"
    assert event.sender_id == "ou_123"
    assert result.ok is True
    assert result.event is event
    assert result.status_code == 200
    assert result.response_body == {"ok": True}


def test_gateway_registry_accepts_inbound_capable_adapter():
    from gateway.contracts import InboundParseResult, SendResult
    from gateway.registry import GatewayRegistry

    class FakeInboundAdapter:
        key = "fake"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

        def parse_callback(self, headers, body):
            return InboundParseResult(ok=True, response_body={"challenge": "ok"}, status_code=200)

    registry = GatewayRegistry()
    adapter = FakeInboundAdapter()
    registry.register(adapter)

    assert registry.get("fake") is adapter
    assert registry.platform_keys() == ["fake"]
