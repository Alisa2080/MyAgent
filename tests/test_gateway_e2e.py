from __future__ import annotations

import json


def test_fake_feishu_callback_to_origin_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "token")

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from gateway.callback_server import CallbackApplication
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore
    from gateway.platforms.feishu import FeishuPlatformAdapter

    sent = []

    class FakeFeishuAdapter(FeishuPlatformAdapter):
        def __init__(self):
            super().__init__(http_sender=lambda url, payload, headers=None: (200, "{}"))

        def send_text(self, target, message):
            from gateway.contracts import SendResult

            sent.append((target, message))
            return SendResult(ok=True)

    registry = GatewayRegistry()
    registry.register(FakeFeishuAdapter())
    origins = []

    def runner(event, session, origin):
        origins.append(origin)
        return "created cron job"

    dispatcher = GatewayDispatcher(
        store=GatewaySessionStore(tmp_path / "gateway.sqlite"),
        registry=registry,
        runner=runner,
    )
    app = CallbackApplication(registry=registry, dispatch=dispatcher.dispatch)
    body = {
        "schema": "2.0",
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "token": "token",
            "create_time": "1780000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": json.dumps({"text": "run a cron report"}),
            },
        },
    }

    response = app.handle("POST", "/callback/feishu", {}, json.dumps(body).encode())

    assert response.status_code == 200
    assert origins[0]["platform"] == "feishu"
    assert sent[0][1].text == "created cron job"

    job = {
        "id": "job-1",
        "name": "Daily",
        "deliver": "origin",
        "origin": origins[0],
    }
    event = enqueue_result(
        job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-06-03T00:00:00+00:00",
    )

    # process_due internally uses default_gateway_registry() which will
    # create a real Feishu adapter. Inject our fake adapter via the
    # factory override pattern so that origin delivery goes through it.
    import gateway.registry as gateway_registry_mod

    fake_adapter = FakeFeishuAdapter()
    gateway_registry_mod.clear_gateway_adapter_factories()
    gateway_registry_mod.register_gateway_adapter_factory(lambda **kwargs: fake_adapter)
    try:
        summary = process_due(limit=10)
    finally:
        gateway_registry_mod.clear_gateway_adapter_factories()

    assert summary["delivered"] == 1
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
    assert sent[-1][0].target_id == "oc_123"
    assert "done" in sent[-1][1].text
