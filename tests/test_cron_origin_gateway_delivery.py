from __future__ import annotations

import json


class FakeGatewayAdapter:
    key = "feishu"

    def __init__(self, *, ok=True, retryable=False, error=None):
        self.ok = ok
        self.retryable = retryable
        self.error = error
        self.sent = []

    def send_text(self, target, message):
        from gateway.contracts import SendResult

        self.sent.append((target, message))
        return SendResult(ok=self.ok, retryable=self.retryable, error=self.error)

    def validate_target(self, target):
        from gateway.contracts import SendResult

        return SendResult(ok=True)


def delivery_event():
    return {
        "id": "evt-1",
        "payload_json": json.dumps(
            {
                "job_id": "job-1",
                "job_name": "Daily",
                "run_id": "run-1",
                "status": "ok",
                "final_response": "done",
                "output_path": "/tmp/out.md",
            }
        ),
        "address": "oc_123",
        "thread_id": "thread-1",
    }


def test_origin_delivery_sends_gateway_origin(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import OriginDeliveryAdapter

    fake = FakeGatewayAdapter()
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        adapter = OriginDeliveryAdapter()
        job = {
            "id": "job-1",
            "origin": {
                "source_type": "gateway",
                "platform": "feishu",
                "chat_id": "oc_123",
                "thread_id": "thread-1",
            },
        }

        result = adapter.deliver(delivery_event(), job, {"id": "run-1"})

        assert result.delivered is True
        target, message = fake.sent[0]
        assert target.platform == "feishu"
        assert target.target_type == "chat_id"
        assert target.target_id == "oc_123"
        assert target.thread_id == "thread-1"
        assert "Daily" in message.text
        assert "done" in message.text
    finally:
        gateway_registry.clear_gateway_adapter_factories()


def test_origin_delivery_maps_retryable_gateway_failure(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import OriginDeliveryAdapter

    fake = FakeGatewayAdapter(ok=False, retryable=True, error="rate limit")
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        result = OriginDeliveryAdapter().deliver(
            delivery_event(),
            {
                "id": "job-1",
                "origin": {"source_type": "gateway", "platform": "feishu", "chat_id": "oc_123"},
            },
            {"id": "run-1"},
        )

        assert result.delivered is False
        assert result.retryable is True
        assert result.error == "rate limit"
    finally:
        gateway_registry.clear_gateway_adapter_factories()


def test_origin_delivery_truncates_long_gateway_message(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import OriginDeliveryAdapter

    fake = FakeGatewayAdapter()
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        event = delivery_event()
        payload = json.loads(event["payload_json"])
        payload["final_response"] = "x" * 5000
        event["payload_json"] = json.dumps(payload)

        result = OriginDeliveryAdapter().deliver(
            event,
            {
                "id": "job-1",
                "origin": {"source_type": "gateway", "platform": "feishu", "chat_id": "oc_123"},
            },
            {"id": "run-1"},
        )

        assert result.delivered is True
        assert len(fake.sent[0][1].text) <= 3900
        assert "truncated" in fake.sent[0][1].text
    finally:
        gateway_registry.clear_gateway_adapter_factories()
