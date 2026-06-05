from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from gateway.contracts import InboundEvent
from gateway.inbox_store import GatewayInboxStore
from gateway.inbox_worker import GatewayInboxWorker
from gateway.platforms.feishu import FeishuPlatformAdapter
from gateway.registry import GatewayRegistry
from gateway.service import GatewayService
from gateway.transports.feishu_ws import normalize_feishu_ws_event

BASE_TIME = "2026-06-02T10:00:00+00:00"


class RecordingFeishuGatewayAdapter:
    key = "feishu"

    def __init__(self):
        from gateway.contracts import SendResult

        self.calls = []
        self.default_result = SendResult(ok=True)

    def validate_target(self, target):
        from gateway.contracts import SendResult

        if target.platform != "feishu":
            return SendResult(ok=False, error="wrong platform")
        if target.target_type != "chat_id":
            return SendResult(ok=False, error="wrong target type")
        if not target.target_id:
            return SendResult(ok=False, error="missing chat_id")
        return SendResult(ok=True)

    def token_smoke(self):
        from gateway.contracts import SendResult

        return SendResult(ok=True)

    def send_text(self, target, message):
        self.calls.append((target, message))
        return self.default_result


class RecordingCronRunner:
    def __init__(self, final_response="scheduled report ready"):
        self.final_response = final_response
        self.calls = []

    def __call__(self, job):
        from cron.contracts import JobRunResult

        self.calls.append(dict(job))
        return JobRunResult(
            success=True,
            output_doc="# Cron Output\nbody",
            final_response=self.final_response,
            error=None,
        )


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


def test_feishu_ws_origin_delivery_sends_cron_result_to_original_chat(monkeypatch, tmp_path):
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
        assert "/im/v1/messages?receive_id_type=chat_id" in sent[-1][0]
        assert "mid-1" not in sent[-1][0]
        assert "Daily" in sent[-1][1]["content"]
        assert "done" in sent[-1][1]["content"]
    finally:
        gateway_registry.clear_gateway_adapter_factories()


def test_feishu_ws_normalize_returns_ignored_reasons():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    base = {
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }

    event, reason = normalize_feishu_ws_event_with_reason(base)
    assert event is not None
    assert reason is None

    payload = {**base, "header": {**base["header"], "event_type": "other.event"}}
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_event_type"

    payload = {
        **base,
        "event": {
            **base["event"],
            "message": {**base["event"]["message"], "message_type": "image"},
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_message_type"

    payload = {
        **base,
        "event": {
            **base["event"],
            "message": {**base["event"]["message"], "content": '{"text":"   "}'},
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "empty_text"


def test_feishu_ws_logs_received_enqueued_and_ignored_events(monkeypatch, tmp_path, caplog):
    import json
    import logging
    import threading
    import types

    import gateway.transports.feishu_ws as feishu_ws

    captured = {}

    class FakeJSON:
        @staticmethod
        def marshal(data):
            return json.dumps(data)

    class FakeBuilder:
        def __init__(self):
            self.callback = None

        def register_p2_im_message_receive_v1(self, callback):
            self.callback = callback
            return self

        def build(self):
            captured["callback"] = self.callback
            return "handler"

    class FakeHandler:
        @staticmethod
        def builder(_verification_token, _encrypt_key):
            return FakeBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            self.event_handler = event_handler

        def start(self):
            captured["started"] = True

        def stop(self):
            captured["stopped"] = True

    fake_lark = types.SimpleNamespace(
        JSON=FakeJSON,
        EventDispatcherHandler=FakeHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(__import__("sys").modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    stop_event = threading.Event()
    stop_event.set()

    with caplog.at_level(logging.INFO, logger="gateway.transports.feishu_ws"):
        feishu_ws.serve_feishu_ws_gateway(home=tmp_path, stop_event=stop_event)

    text_payload = {
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    ignored_payload = {
        "header": {"event_id": "evt-2", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-2",
                "chat_id": "oc_123",
                "message_type": "image",
                "content": "{}",
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="gateway.transports.feishu_ws"):
        captured["callback"](text_payload)
        captured["callback"](ignored_payload)

    assert "Feishu WS: received event" in caplog.text
    assert "Feishu WS: enqueued" in caplog.text
    assert "Feishu WS: ignored event" in caplog.text
    assert "unsupported_message_type" in caplog.text
    assert "hello" not in caplog.text


def test_feishu_inbound_creates_origin_cron_and_delivers_report_to_chat(monkeypatch, tmp_path):
    import cron.delivery_store as delivery_store_module
    import cron.state_store as state_store_module
    import gateway.registry as gateway_registry
    from agent_tools.public.cronjob import run_cronjob_action
    from cron.delivery_store import DeliveryStore
    from cron.jobs import get_job
    from cron.service import CronService
    import cron.scheduler as scheduler
    from cron.state_store import StateStore
    from gateway.dispatch import GatewayDispatcher, gateway_agent_config
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    gateway_registry.clear_gateway_adapter_factories()
    feishu = RecordingFeishuGatewayAdapter()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: feishu)

    registry = GatewayRegistry()
    registry.register(feishu)
    created = {}

    def gateway_runner(event, session, origin):
        runtime = SimpleNamespace(config=gateway_agent_config(origin))
        result = run_cronjob_action(
            "create",
            runtime=runtime,
            schedule="every 30m",
            prompt="write the Feishu report",
            name="Feishu Report",
            deliver="origin",
        )
        assert result["success"] is True
        created["job_id"] = result["job_id"]
        return "Cron job Feishu Report created."

    try:
        dispatcher = GatewayDispatcher(
            store=GatewaySessionStore(tmp_path / "gateway" / "gateway.sqlite"),
            registry=registry,
            runner=gateway_runner,
        )
        service = GatewayService(home=tmp_path, registry=registry, dispatch=dispatcher.dispatch)
        inbox = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
        payload = {
            "header": {
                "event_id": "evt-e2e",
                "event_type": "im.message.receive_v1",
                "create_time": "1760000000000",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}, "sender_name": "Miku"},
                "message": {
                    "message_id": "mid-create",
                    "chat_id": "oc_e2e",
                    "message_type": "text",
                    "content": '{"text":"create a report every 30 minutes"}',
                },
            },
        }

        event = normalize_feishu_ws_event(payload)
        assert event is not None
        inbox.enqueue(event)
        processed = GatewayInboxWorker(store=inbox, dispatch=service.handle_event, sleeper=lambda _: None).run_once()

        assert processed == 1
        job = get_job(created["job_id"])
        assert job is not None
        assert job["deliver"] == "origin"
        assert job["origin"]["source_type"] == "gateway"
        assert job["origin"]["platform"] == "feishu"
        assert job["origin"]["chat_id"] == "oc_e2e"
        assert job["origin"]["thread_id"] == "mid-create"
        assert job["origin"]["session_id"]

        # Force the job due
        from cron.jobs import update_job
        from cron.state_store import _parse_time

        due_time = _parse_time(BASE_TIME)
        due_job = update_job(
            job["id"],
            {"next_run_at": due_time.isoformat() if due_time else BASE_TIME, "state": "scheduled", "enabled": True},
        )
        assert due_job is not None

        now = datetime.fromisoformat(BASE_TIME)
        original_state_utc_now = state_store_module.utc_now
        original_delivery_utc_now = delivery_store_module.utc_now
        runner = RecordingCronRunner(final_response="scheduled report ready")
        tick_result = None

        def tick_fn():
            nonlocal tick_result
            tick_result = scheduler.tick(now_text=BASE_TIME, job_runner=runner)
            return tick_result

        try:
            state_store_module.utc_now = lambda: now
            delivery_store_module.utc_now = lambda: now
            cron_service = CronService(
                interval_seconds=1,
                lease_seconds=60,
                owner_id="gateway-e2e",
                pid=4242,
                hostname="gateway-e2e-host",
                tick_fn=tick_fn,
                clock=lambda: BASE_TIME,
                sleeper=lambda _seconds: None,
            )
            exit_code = cron_service.run(once=True)
        finally:
            state_store_module.utc_now = original_state_utc_now
            delivery_store_module.utc_now = original_delivery_utc_now

        store = StateStore()
        delivery_store = DeliveryStore()
        runs = store.runs_for_job(job["id"])
        events = delivery_store.list_events(job_id=job["id"], limit=10)
        target, message = feishu.calls[-1]

        assert exit_code == 0
        assert tick_result.ran == 1
        assert len(runner.calls) == 1
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["delivery_status"] == "delivered"
        assert len(events) == 1
        assert events[0]["adapter_key"] == "origin"
        assert events[0]["status"] == "delivered"
        assert target.platform == "feishu"
        assert target.target_type == "chat_id"
        assert target.target_id == "oc_e2e"
        assert target.thread_id is None
        assert "scheduled report ready" in message.text
    finally:
        gateway_registry.clear_gateway_adapter_factories()
