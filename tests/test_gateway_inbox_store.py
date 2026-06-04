from __future__ import annotations

import time


def test_enqueue_idempotent_same_row_id(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="im.message.receive_v1",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-01-01T00:00:00Z",
        thread_id="th-1",
        sender_id="ou_1",
        sender_name="Miku",
        raw={"foo": "bar"},
    )

    id1 = store.enqueue(ev)
    id2 = store.enqueue(ev)

    assert id1 == id2

    stats = store.stats()
    assert stats["pending"] == 1
    assert stats["processing"] == 0
    assert stats["succeeded"] == 0
    assert stats["failed"] == 0
    assert stats["dead"] == 0


def test_inbox_store_does_not_export_legacy_alias():
    import gateway.inbox_store as inbox_store

    assert hasattr(inbox_store, "GatewayInboxStore")
    assert not hasattr(inbox_store, "InboxStore")


def test_claim_due_marks_processing_and_returns_rows(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev1 = InboundEvent(
        platform="feishu", event_id="e1", event_type="type",
        chat_id="c1", text="t1", timestamp="2026-01-01T00:00:00Z",
    )
    ev2 = InboundEvent(
        platform="feishu", event_id="e2", event_type="type",
        chat_id="c1", text="t2", timestamp="2026-01-01T00:00:00Z",
    )
    ev3 = InboundEvent(
        platform="feishu", event_id="e3", event_type="type",
        chat_id="c1", text="t3", timestamp="2026-01-01T00:00:00Z",
    )

    store.enqueue(ev1)
    store.enqueue(ev2)
    store.enqueue(ev3)

    rows = store.claim_due(limit=2)
    assert len(rows) == 2

    stats = store.stats()
    assert stats["pending"] == 1
    assert stats["processing"] == 2


def test_complete_marks_succeeded_and_stats_update(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev = InboundEvent(
        platform="feishu", event_id="e1", event_type="type",
        chat_id="c1", text="hello", timestamp="2026-01-01T00:00:00Z",
    )
    row_id = store.enqueue(ev)
    rows = store.claim_due(limit=1)
    assert len(rows) == 1
    assert rows[0].id == row_id

    store.complete(row_id)

    stats = store.stats()
    assert stats["pending"] == 0
    assert stats["processing"] == 0
    assert stats["succeeded"] == 1
    assert stats["failed"] == 0
    assert stats["dead"] == 0

    item = store.get(row_id)
    assert item.status == "succeeded"
    assert item.last_error is None


def test_fail_then_retry_then_dead(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev = InboundEvent(
        platform="feishu", event_id="e1", event_type="type",
        chat_id="c1", text="hello", timestamp="2026-01-01T00:00:00Z",
    )
    row_id = store.enqueue(ev)
    rows = store.claim_due(limit=1)
    assert len(rows) == 1

    # First fail: transitions to failed (attempts=1, next_attempt_at set ~1s in future)
    store.fail(row_id, "oops", retry_delays=[1])
    stats = store.stats()
    assert stats["failed"] == 1
    assert stats["pending"] == 0

    item = store.get(row_id)
    assert item.attempts == 1
    assert item.last_error == "oops"

    # Wait for retry window to open
    time.sleep(1.1)

    # Claim again (now max_attempts=2 means still alive)
    rows2 = store.claim_due(limit=10)
    assert len(rows2) == 1
    assert rows2[0].id == row_id

    # Second fail with max_attempts=2 -> dead
    store.fail(row_id, "oops again", retry_delays=[60], max_attempts=2)
    stats = store.stats()
    assert stats["dead"] == 1

    item = store.get(row_id)
    assert item.attempts == 2
    assert item.status == "dead"


def test_recover_stale_processing(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev = InboundEvent(
        platform="feishu", event_id="e1", event_type="type",
        chat_id="c1", text="hello", timestamp="2026-01-01T00:00:00Z",
    )
    store.enqueue(ev)
    rows = store.claim_due(limit=1)
    assert len(rows) == 1

    # Simulate stale: overwrite claimed_at to the past
    with store.connect() as conn:
        conn.execute(
            "UPDATE gateway_inbox SET claimed_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (rows[0].id,),
        )

    recovered = store.recover_stale_processing()
    assert recovered == 1

    stats = store.stats()
    assert stats["failed"] == 1
    assert stats["processing"] == 0


def test_get_returns_row(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ev = InboundEvent(
        platform="feishu", event_id="e1", event_type="im.message.receive_v1",
        chat_id="oc_chat", text="hello world", timestamp="2026-01-01T00:00:00Z",
        thread_id="th_1", sender_id="ou_sender", sender_name="Miku",
        raw={"key": "value"},
    )
    row_id = store.enqueue(ev)

    item = store.get(row_id)
    assert item is not None
    assert item.id == row_id
    assert item.platform == "feishu"
    assert item.event_id == "e1"
    assert item.event_type == "im.message.receive_v1"
    assert item.chat_id == "oc_chat"
    assert item.thread_id == "th_1"
    assert item.sender_id == "ou_sender"
    assert item.sender_name == "Miku"
    assert item.text == "hello world"
    assert item.raw_json == {"key": "value"}
    assert item.status == "pending"
    assert item.attempts == 0
    assert item.claimed_at is None
    assert item.next_attempt_at is None
    assert item.last_error is None


def test_stats_empty_store(tmp_path):
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    stats = store.stats()

    assert stats["pending"] == 0
    assert stats["processing"] == 0
    assert stats["succeeded"] == 0
    assert stats["failed"] == 0
    assert stats["dead"] == 0


def test_enqueued_items_sorted_by_created_at(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.contracts import InboundEvent

    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    ids = []
    for i in range(5):
        ev = InboundEvent(
            platform="feishu", event_id=f"e{i}", event_type="type",
            chat_id="c1", text=f"t{i}", timestamp="2026-01-01T00:00:00Z",
        )
        ids.append(store.enqueue(ev))

    rows = store.claim_due(limit=10)
    claimed_ids = [r.id for r in rows]

    assert set(claimed_ids) == set(ids)
    assert claimed_ids == ids
