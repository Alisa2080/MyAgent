from __future__ import annotations

from gateway.contracts import InboundEvent
from gateway.inbox_store import GatewayInboxStore
from gateway.inbox_worker import GatewayInboxWorker
from gateway.platforms.feishu import FeishuPlatformAdapter
from gateway.registry import GatewayRegistry
from gateway.service import GatewayService
from gateway.transports.feishu_ws import normalize_feishu_ws_event


def test_feishu_ws_inbox_dispatch_preserves_origin_thread(monkeypatch, tmp_path):
    """A feishu-ws event goes through the inbox and dispatches with correct origin thread_id."""
    sent = []

    def fake_sender(url, payload, headers=None):
        if "tenant_access_token" in url:
            return 200, '{"code":0,"tenant_access_token":"token","expire":3600}'
        sent.append((url, payload))
        return 200, '{"code":0}'

    registry = GatewayRegistry()
    registry.register(FeishuPlatformAdapter(http_sender=fake_sender))

    origins = []

    def dispatch(event: InboundEvent):
        origins.append({
            "source_type": "gateway",
            "platform": event.platform,
            "chat_id": event.chat_id,
            "thread_id": event.thread_id,
        })

    service = GatewayService(home=tmp_path, registry=registry, dispatch=dispatch)
    inbox = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_1"},
                "sender_name": "Miku",
            },
            "message": {
                "message_id": "mid-1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"create cron"}',
            },
        },
    }

    event = normalize_feishu_ws_event(payload)
    assert event is not None, "normalize_feishu_ws_event should parse the payload"

    inbox.enqueue(event)

    worker = GatewayInboxWorker(store=inbox, dispatch=service.handle_event, sleeper=lambda _: None)
    processed = worker.run_once()

    assert processed == 1, f"Expected 1 event processed, got {processed}"
    assert len(origins) == 1, f"Expected 1 origin dispatched, got {len(origins)}"
    assert origins[0]["thread_id"] == "mid-1", f"Expected thread_id='mid-1', got {origins[0]['thread_id']}"
    assert origins[0]["chat_id"] == "oc_123"
    assert origins[0]["platform"] == "feishu"


def test_feishu_ws_origin_delivery_sends_cron_result_to_original_thread(monkeypatch, tmp_path):
    from cron.contracts import JobRunResult
    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker
    from gateway.platforms.feishu import FeishuPlatformAdapter
    from gateway.registry import GatewayRegistry
    from gateway.service import GatewayService
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    sent = []

    def fake_sender(url, payload, headers=None):
        if "tenant_access_token" in url:
            return 200, '{"code":0,"tenant_access_token":"token","expire":3600}'
        sent.append((url, payload))
        return 200, '{"code":0}'

    registry = GatewayRegistry()
    adapter = FeishuPlatformAdapter(http_sender=fake_sender)
    registry.register(adapter)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    import gateway.registry as gateway_registry

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: adapter)

    origins = []

    def dispatch(event: InboundEvent):
        origin = {
            "source_type": "gateway",
            "platform": event.platform,
            "chat_id": event.chat_id,
            "thread_id": event.thread_id,
        }
        origins.append(origin)

    try:
        service = GatewayService(home=tmp_path, registry=registry, dispatch=dispatch)
        inbox = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
        payload = {
            "header": {
                "event_id": "evt-1",
                "event_type": "im.message.receive_v1",
                "create_time": "1760000000000",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}, "sender_name": "Miku"},
                "message": {
                    "message_id": "mid-1",
                    "chat_id": "oc_123",
                    "message_type": "text",
                    "content": '{"text":"create cron"}',
                },
            },
        }

        event = normalize_feishu_ws_event(payload)
        assert event is not None
        inbox.enqueue(event)
        GatewayInboxWorker(store=inbox, dispatch=service.handle_event, sleeper=lambda _: None).run_once()

        store = DeliveryStore(tmp_path / "delivery.sqlite")
        job = {
            "id": "job-1",
            "name": "Daily",
            "deliver": "origin",
            "origin": origins[0],
        }
        enqueue_result(
            job,
            JobRunResult(success=True, final_response="done", error=None),
            "/tmp/out.md",
            "2026-06-03T00:00:00+00:00",
            store=store,
        )
        process_due(store=store)

        assert sent
        assert any("mid-1" in item[0] for item in sent)
        assert "Daily" in sent[-1][1]["content"]
        assert "done" in sent[-1][1]["content"]
    finally:
        gateway_registry.clear_gateway_adapter_factories()
