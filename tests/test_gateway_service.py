from __future__ import annotations

from gateway.contracts import InboundEvent


def test_gateway_service_dispatches_event_and_writes_status(tmp_path):
    from gateway.service import GatewayService
    from gateway.service_state import read_gateway_status

    dispatched = []
    service = GatewayService(
        home=tmp_path,
        registry=None,
        dispatch=dispatched.append,
    )
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )
    service.handle_event(event)
    service.write_status(process_state="running")
    assert dispatched == [event]
    status = read_gateway_status(tmp_path)
    assert status["process_state"] == "running"
    assert status["service"] == "gateway"


def test_gateway_service_default_dispatch_uses_gateway_dispatcher(tmp_path, monkeypatch):
    from gateway.service import GatewayService

    calls = []

    class FakeDispatcher:
        def __init__(self, *, store, registry):
            calls.append(("init", store.db_path, registry.platform_keys()))

        def dispatch(self, event):
            calls.append(("dispatch", event.event_id))

    monkeypatch.setattr("gateway.service.GatewayDispatcher", FakeDispatcher)
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )

    service = GatewayService(home=tmp_path)
    service.handle_event(event)

    assert calls[0][0] == "init"
    assert calls[0][1] == tmp_path / "gateway" / "gateway.sqlite"
    assert "feishu" in calls[0][2]
    assert calls[1] == ("dispatch", "evt-1")


def test_gateway_service_raises_when_dispatch_result_fails(tmp_path):
    from gateway.dispatch import DispatchResult
    from gateway.service import GatewayService

    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )
    service = GatewayService(
        home=tmp_path,
        dispatch=lambda event: DispatchResult(False, "gw_1", error="send failed"),
    )

    try:
        service.handle_event(event)
    except RuntimeError as exc:
        assert str(exc) == "send failed"
    else:
        raise AssertionError("expected dispatch failure to raise")
