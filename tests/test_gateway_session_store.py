from __future__ import annotations


def test_gateway_session_identity_reuses_chat_thread(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")

    first = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
    )
    second = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_2",
        sender_name="Other",
    )
    third = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-2",
        sender_id="ou_1",
        sender_name="Miku",
    )

    assert first.session_id == second.session_id
    assert third.session_id != first.session_id
    assert second.sender_id == "ou_1"


def test_gateway_session_store_records_messages_in_order(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    session = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id=None,
        sender_id="ou_1",
        sender_name="Miku",
    )

    store.record_message(
        session.session_id,
        direction="inbound",
        platform="feishu",
        event_id="evt-1",
        text="hello",
        raw={"a": 1},
    )
    store.record_message(
        session.session_id,
        direction="outbound",
        platform="feishu",
        event_id=None,
        text="world",
        raw={},
    )

    messages = store.list_messages(session.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]
    assert [message.text for message in messages] == ["hello", "world"]
    refreshed = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id=None,
        sender_id="ou_1",
        sender_name="Miku",
    )
    assert refreshed.last_event_id == "evt-1"
    assert refreshed.last_message_preview == "world"


def test_gateway_event_dedupe_is_platform_scoped(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")

    assert store.claim_event("feishu", "evt-1") is True
    assert store.claim_event("feishu", "evt-1") is False
    assert store.claim_event("slack", "evt-1") is True


def test_gateway_session_store_pending_clarify_lifecycle(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    session = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id=None,
        sender_id="ou_1",
        sender_name="Miku",
    )
    payload = {
        "action_request": {
            "name": "clarify",
            "args": {
                "question": "Which path?",
                "choices": ["Small", "Complete"],
            },
        },
        "review_config": {"kind": "clarify"},
    }

    store.set_pending_interrupt(session.session_id, kind="clarify", payload=payload)
    pending = store.get_pending_interrupt(session.session_id)

    assert pending is not None
    assert pending.kind == "clarify"
    assert pending.payload["action_request"]["args"]["question"] == "Which path?"

    store.clear_pending_interrupt(session.session_id)
    assert store.get_pending_interrupt(session.session_id) is None
