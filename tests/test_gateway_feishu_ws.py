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
