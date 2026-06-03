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