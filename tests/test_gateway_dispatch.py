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


def test_gateway_dispatch_sends_fallback_when_runner_returns_no_text(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(
        store=store,
        registry=registry,
        runner=lambda event, session, origin: None,
    )

    result = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-empty",
            event_type="message",
            chat_id="oc_123",
            text="explain cron architecture",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )

    assert result.ok is True
    assert adapter.sent
    target, message = adapter.sent[0]
    assert target.target_id == "oc_123"
    assert "无法生成回复" in message.text
    messages = store.list_messages(result.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]
    assert "无法生成回复" in messages[-1].text


def test_gateway_dispatch_does_not_claim_event_when_runner_fails(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    calls = []
    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)

    def flaky_runner(event, session, origin):
        calls.append(event.event_id)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return "agent response"

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=flaky_runner)
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )

    try:
        dispatcher.dispatch(event)
    except RuntimeError:
        pass
    second = dispatcher.dispatch(event)
    third = dispatcher.dispatch(event)

    assert second.ok is True
    assert third.duplicate is True
    assert calls == ["evt-1", "evt-1"]


def test_gateway_dispatch_marks_inflight_event_duplicate(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    calls = []
    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )

    def runner(inbound_event, session, origin):
        calls.append(inbound_event.event_id)
        duplicate = dispatcher.dispatch(event)
        assert duplicate.duplicate is True
        return "agent response"

    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=runner)

    result = dispatcher.dispatch(event)

    assert result.ok is True
    assert calls == ["evt-1"]


def test_gateway_dispatch_recovers_stale_processing_event(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    calls = []
    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    assert store.begin_event_processing("feishu", "evt-1") is True
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE gateway_inbound_events
            SET claimed_at = '2026-06-03T00:00:00+00:00'
            WHERE platform = 'feishu' AND event_id = 'evt-1'
            """
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
    dispatcher = GatewayDispatcher(
        store=store,
        registry=registry,
        runner=lambda inbound_event, session, origin: calls.append(inbound_event.event_id) or "agent response",
    )

    result = dispatcher.dispatch(event)

    assert result.ok is True
    assert result.duplicate is False
    assert calls == ["evt-1"]


def test_default_gateway_runner_passes_origin_config_to_agent():
    from gateway.dispatch import run_gateway_agent_turn
    from gateway.session_store import GatewaySession

    class FakeAgent:
        def __init__(self):
            self.calls = []

        def invoke(self, input_data, config=None):
            self.calls.append((input_data, config))
            return {"messages": [{"role": "assistant", "content": "ok"}]}

    agent = FakeAgent()
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
        raw={},
    )
    session = GatewaySession(
        session_id="gw_1",
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
        status="active",
        last_event_id=None,
        last_message_preview=None,
    )
    origin = {
        "source_type": "gateway",
        "platform": "feishu",
        "chat_id": "oc_123",
        "thread_id": "thread-1",
        "sender_id": "ou_1",
        "display_name": "Miku",
        "session_id": "gw_1",
    }

    result = run_gateway_agent_turn(event, session, origin, agent=agent)

    assert result == {"messages": [{"role": "assistant", "content": "ok"}]}
    input_data, config = agent.calls[0]
    assert input_data == {"messages": [{"role": "user", "content": "hello"}]}
    assert config["configurable"]["thread_id"] == "gw_1"
    assert config["configurable"]["source_type"] == "gateway"
    assert config["configurable"]["platform"] == "feishu"
    assert config["configurable"]["chat_id"] == "oc_123"
    assert config["configurable"]["session_id"] == "gw_1"
    assert config["configurable"]["display_name"] == "Miku"


def test_default_gateway_runner_resumes_pending_interrupt():
    from gateway.dispatch import run_gateway_agent_turn
    from gateway.session_store import GatewaySession

    class FakeAgent:
        def __init__(self):
            self.calls = []

        def invoke(self, input_data, config=None):
            self.calls.append((input_data, config))
            return {"messages": [{"role": "assistant", "content": "resumed"}]}

    agent = FakeAgent()
    event = InboundEvent(
        platform="feishu",
        event_id="evt-2",
        event_type="message",
        chat_id="oc_123",
        text="2",
        timestamp="2026-06-05T00:01:00+00:00",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
        raw={},
    )
    session = GatewaySession(
        session_id="gw_1",
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
        status="active",
        last_event_id=None,
        last_message_preview=None,
    )
    origin = {
        "source_type": "gateway",
        "platform": "feishu",
        "chat_id": "oc_123",
        "thread_id": "thread-1",
        "sender_id": "ou_1",
        "display_name": "Miku",
        "session_id": "gw_1",
        "resume": {"decisions": [{"type": "respond", "message": "Complete"}]},
    }

    result = run_gateway_agent_turn(event, session, origin, agent=agent)

    assert result == {"messages": [{"role": "assistant", "content": "resumed"}]}
    input_data, config = agent.calls[0]
    assert input_data.resume == {"decisions": [{"type": "respond", "message": "Complete"}]}
    assert config["configurable"]["thread_id"] == "gw_1"


def _clarify_interrupt_result():
    return {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {
                            "name": "clarify",
                            "args": {
                                "question": "Which path?",
                                "choices": ["Small", "Complete"],
                            },
                        }
                    ],
                    "review_configs": [
                        {
                            "kind": "clarify",
                            "allowed_decisions": ["respond"],
                        }
                    ],
                }
            }
        ]
    }


def test_gateway_dispatch_sends_clarify_and_records_pending(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(
        store=store,
        registry=registry,
        runner=lambda event, session, origin: _clarify_interrupt_result(),
    )

    result = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-clarify",
            event_type="message",
            chat_id="oc_123",
            text="build it",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )

    assert result.ok is True
    assert adapter.sent
    assert "Which path?" in adapter.sent[0][1].text
    assert "1. Small" in adapter.sent[0][1].text
    assert "Other: type your answer" in adapter.sent[0][1].text
    pending = store.get_pending_interrupt(result.session_id)
    assert pending is not None
    assert pending.kind == "clarify"
    messages = store.list_messages(result.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]


def test_gateway_dispatch_next_message_answers_pending_clarify(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    calls = []

    def runner(event, session, origin):
        calls.append(event.text)
        if len(calls) == 1:
            return _clarify_interrupt_result()
        return "resumed with answer"

    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=runner)
    first = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="message",
            chat_id="oc_123",
            text="build it",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )
    second = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-2",
            event_type="message",
            chat_id="oc_123",
            text="2",
            timestamp="2026-06-05T00:01:00+00:00",
            raw={},
        )
    )

    assert first.ok is True
    assert second.ok is True
    assert adapter.sent[-1][1].text == "resumed with answer"
    assert store.get_pending_interrupt(first.session_id) is None


def test_gateway_dispatch_resume_can_store_followup_clarify(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    calls = []

    def runner(event, session, origin):
        calls.append(origin)
        return _clarify_interrupt_result()

    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=runner)
    first = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="message",
            chat_id="oc_123",
            text="build it",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )
    second = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-2",
            event_type="message",
            chat_id="oc_123",
            text="2",
            timestamp="2026-06-05T00:01:00+00:00",
            raw={},
        )
    )

    assert first.ok is True
    assert second.ok is True
    assert len(adapter.sent) == 2
    assert "Which path?" in adapter.sent[-1][1].text
    assert store.get_pending_interrupt(first.session_id) is not None
