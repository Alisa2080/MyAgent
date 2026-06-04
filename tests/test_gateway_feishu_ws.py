def _payload(message_type="text", content='{"text":"hello"}', event_id="evt-1"):
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_1"},
                "sender_name": "Miku",
            },
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": message_type,
                "content": content,
            },
        },
    }


def test_feishu_ws_normalizes_text_message():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    event = normalize_feishu_ws_event(_payload())

    assert event is not None
    assert event.platform == "feishu"
    assert event.event_id == "evt-1"
    assert event.chat_id == "oc_123"
    assert event.thread_id == "thread_1"
    assert event.sender_id == "ou_1"
    assert event.text == "hello"


def test_feishu_ws_uses_message_id_as_thread_fallback():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    payload = _payload()
    payload["event"]["message"].pop("thread_id")

    event = normalize_feishu_ws_event(payload)

    assert event.thread_id == "mid_1"


def test_feishu_ws_ignores_non_text_message():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    assert normalize_feishu_ws_event(_payload(message_type="image")) is None


def test_feishu_ws_ignores_missing_required_ids():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    payload = _payload()
    payload["event"]["message"].pop("chat_id")

    assert normalize_feishu_ws_event(payload) is None


def test_serve_feishu_ws_gateway_stop_event_stops_client(monkeypatch, tmp_path):
    import sys
    import threading
    import types

    import gateway.transports.feishu_ws as feishu_ws

    stopped = threading.Event()
    started = threading.Event()
    clients = []

    class FakeJson:
        @staticmethod
        def marshal(data):
            return "{}"

    class FakeHandlerBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            return self

        def build(self):
            return object()

    class FakeDispatcherHandler:
        @staticmethod
        def builder(app_id, app_secret):
            return FakeHandlerBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            clients.append(self)

        def start(self):
            started.set()
            stopped.wait(timeout=2)

        def stop(self):
            stopped.set()

    fake_lark = types.SimpleNamespace(
        JSON=FakeJson,
        EventDispatcherHandler=FakeDispatcherHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    stop_event = threading.Event()
    runner = threading.Thread(
        target=feishu_ws.serve_feishu_ws_gateway,
        kwargs={"home": tmp_path, "stop_event": stop_event},
    )
    runner.start()
    assert started.wait(timeout=2)

    stop_event.set()
    runner.join(timeout=2)

    assert not runner.is_alive()
    assert clients
    assert stopped.is_set()


# ---------------------------------------------------------------------------
# Task 3: Feishu WS Diagnostic Logging – normalization reasons & stale events
# ---------------------------------------------------------------------------


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_post_message_type():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "post",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_message_type"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_unsupported_message_type():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "image",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_message_type"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_unsupported_event_type():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.p2pchat.read.v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_event_type"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_missing_chat_id():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "missing_chat_id"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_missing_message_id():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "missing_message_id"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_missing_event_id():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "missing_event_id"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_empty_text():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":""}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "empty_text"


def test_normalize_feishu_ws_event_with_reason_returns_event_for_valid_text():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = {
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_name": "Miku"},
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is not None
    assert reason is None
    assert event.event_id == "evt-1"
    assert event.chat_id == "oc_123"


def test_normalize_feishu_ws_event_with_reason_returns_reason_for_malformed_payload():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    payload = "not-a-dict"
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "malformed_payload"


def test_on_message_logs_received_and_enqueued_for_valid_event(monkeypatch, caplog, tmp_path):
    import logging
    import sys
    import types

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '{"header":{"event_id":"evt-1","event_type":"im.message.receive_v1","create_time":"1760000000000"},"event":{"sender":{"sender_id":{"open_id":"ou_1"},"sender_name":"Miku"},"message":{"message_id":"mid_1","thread_id":"thread_1","chat_id":"oc_123","message_type":"text","content":"{\\"text\\":\\"hello\\"}"}}}'

    class FakeHandlerBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            self._cb = callback
            return self

        def build(self):
            return object()

        def trigger(self, data):
            return self._cb(data)

    class FakeDispatcherHandler:
        @staticmethod
        def builder(app_id, app_secret):
            return FakeHandlerBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    fake_lark = types.SimpleNamespace(
        JSON=FakeJson,
        EventDispatcherHandler=FakeDispatcherHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from gateway.inbox_store import GatewayInboxStore

    inbox = GatewayInboxStore(tmp_path / "gateway.sqlite")

    handler_builder = FakeDispatcherHandler.builder("", "")
    on_message = feishu_ws._make_on_message(inbox, logging.getLogger())
    handler_builder.register_p2_im_message_receive_v1(on_message)
    handler = handler_builder.build()

    class FakeData:
        pass

    data = FakeData()
    with caplog.at_level(logging.INFO):
        on_message(data)

    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    received = [(lvl, msg) for lvl, msg in records if "received" in msg or "enqueued" in msg]
    assert len(received) >= 1, f"expected received/enqueued log; got {records}"


def test_on_message_logs_ignored_reason_for_unsupported_message_type(monkeypatch, caplog, tmp_path):
    import logging
    import sys
    import types

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '{"header":{"event_id":"evt-1","event_type":"im.message.receive_v1","create_time":"1760000000000"},"event":{"sender":{"sender_id":{"open_id":"ou_1"}},"message":{"message_id":"mid_1","thread_id":"thread_1","chat_id":"oc_123","message_type":"image","content":"{}"}}}'

    class FakeHandlerBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            self._cb = callback
            return self

        def build(self):
            return object()

    class FakeDispatcherHandler:
        @staticmethod
        def builder(app_id, app_secret):
            return FakeHandlerBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    fake_lark = types.SimpleNamespace(
        JSON=FakeJson,
        EventDispatcherHandler=FakeDispatcherHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from gateway.inbox_store import GatewayInboxStore

    inbox = GatewayInboxStore(tmp_path / "gateway.sqlite")

    class FakeData:
        pass

    with caplog.at_level(logging.WARNING):
        on_message = feishu_ws._make_on_message(inbox, logging.getLogger())
        on_message(FakeData())

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unsupported_message_type" in msg or "ignore" in msg.lower() for msg in warning_msgs), (
        f"expected WARNING log with ignore reason; got {warning_msgs}"
    )


def test_on_message_logs_ignored_reason_for_missing_chat_id(monkeypatch, caplog, tmp_path):
    import logging
    import sys
    import types

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '{"header":{"event_id":"evt-1","event_type":"im.message.receive_v1","create_time":"1760000000000"},"event":{"sender":{"sender_id":{"open_id":"ou_1"}},"message":{"message_id":"mid_1","thread_id":"thread_1","message_type":"text","content":"{\\"text\\":\\"hello\\"}"}}}'

    class FakeHandlerBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            self._cb = callback
            return self

        def build(self):
            return object()

    class FakeDispatcherHandler:
        @staticmethod
        def builder(app_id, app_secret):
            return FakeHandlerBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    fake_lark = types.SimpleNamespace(
        JSON=FakeJson,
        EventDispatcherHandler=FakeDispatcherHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from gateway.inbox_store import GatewayInboxStore

    inbox = GatewayInboxStore(tmp_path / "gateway.sqlite")

    class FakeData:
        pass

    with caplog.at_level(logging.WARNING):
        on_message = feishu_ws._make_on_message(inbox, logging.getLogger())
        on_message(FakeData())

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("missing_chat_id" in msg for msg in warning_msgs), (
        f"expected WARNING log with 'missing_chat_id'; got {warning_msgs}"
    )


def test_on_message_logs_malformed_payload_without_crashing(monkeypatch, caplog, tmp_path):
    import logging
    import sys
    import types

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '["not", "a", "dict"]'

    fake_lark = types.SimpleNamespace(JSON=FakeJson)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)

    from gateway.inbox_store import GatewayInboxStore

    inbox = GatewayInboxStore(tmp_path / "gateway.sqlite")
    on_message = feishu_ws._make_on_message(inbox, logging.getLogger())

    with caplog.at_level(logging.WARNING):
        on_message(object())

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed_payload" in msg for msg in warning_msgs), warning_msgs
    assert inbox.stats().get("pending", 0) == 0


def test_on_message_logs_enqueue_failure_with_event_metadata(monkeypatch, caplog):
    import logging
    import sys
    import types

    import pytest

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '{"header":{"event_id":"evt-1","event_type":"im.message.receive_v1","create_time":"1760000000000"},"event":{"sender":{"sender_id":{"open_id":"ou_1"}},"message":{"message_id":"mid_1","thread_id":"thread_1","chat_id":"oc_123","message_type":"text","content":"{\\"text\\":\\"hello\\"}"}}}'

    class BrokenInbox:
        def enqueue(self, event):
            raise RuntimeError("sqlite locked")

    fake_lark = types.SimpleNamespace(JSON=FakeJson)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)

    on_message = feishu_ws._make_on_message(BrokenInbox(), logging.getLogger())

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="sqlite locked"):
        on_message(object())

    error_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("evt-1" in msg and "oc_123" in msg and "mid_1" in msg for msg in error_msgs), error_msgs


def test_inbox_stale_processing_recovery(tmp_path):
    from datetime import datetime, timezone

    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "gateway.sqlite")
    row_id = store.enqueue(
        InboundEvent(
            platform="feishu",
            event_id="evt-stale",
            event_type="im.message.receive_v1",
            chat_id="oc_123",
            text="hello",
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw={},
        )
    )

    rows = store.claim_due(limit=1)
    assert len(rows) == 1
    assert rows[0].id == row_id

    with store.connect() as conn:
        conn.execute(
            "UPDATE gateway_inbox SET claimed_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (row_id,),
        )

    recovered = store.recover_stale_processing(stale_seconds=300)

    assert recovered == 1
    item = store.get(row_id)
    assert item is not None
    assert item.status == "failed"
    assert "stale" in (item.last_error or "").lower()


def test_on_message_does_not_enqueue_ignored_events(monkeypatch, tmp_path):
    import sys
    import types

    import gateway.transports.feishu_ws as feishu_ws

    class FakeJson:
        @staticmethod
        def marshal(data):
            return '{"header":{"event_id":"evt-1","event_type":"im.message.receive_v1","create_time":"1760000000000"},"event":{"sender":{"sender_id":{"open_id":"ou_1"}},"message":{"message_id":"mid_1","thread_id":"thread_1","chat_id":"oc_123","message_type":"image","content":"{}"}}}'

    class FakeHandlerBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            self._cb = callback
            return self

        def build(self):
            return object()

    class FakeDispatcherHandler:
        @staticmethod
        def builder(app_id, app_secret):
            return FakeHandlerBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    fake_lark = types.SimpleNamespace(
        JSON=FakeJson,
        EventDispatcherHandler=FakeDispatcherHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from gateway.inbox_store import GatewayInboxStore

    inbox = GatewayInboxStore(tmp_path / "gateway.sqlite")

    class FakeData:
        pass

    on_message = feishu_ws._make_on_message(inbox, __import__("logging").getLogger())
    on_message(FakeData())

    assert inbox.stats().get("pending", 0) == 0, "ignored events must not be enqueued"
