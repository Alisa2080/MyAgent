from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gateway.contracts import InboundEvent
from gateway.inbox_store import GatewayInboxStore, InboxItem
from gateway.inbox_worker import GatewayInboxWorker


class FakeAdapter:
    key = "feishu"

    def send_text(self, target, message):
        return type("SendResult", (), {"ok": True})()


def _make_event(**overrides) -> InboundEvent:
    defaults = dict(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        thread_id=None,
        sender_id=None,
        sender_name=None,
        raw={},
    )
    defaults.update(overrides)
    return InboundEvent(**defaults)


def _enqueue(store: GatewayInboxStore, **overrides) -> str:
    event = _make_event(**overrides)
    return store.enqueue(event)


class FakeDispatch:
    def __init__(self):
        self.calls: list[InboundEvent] = []
        self.raise_on: set[str] = set()
        self.results: dict[str, object] = {}

    def __call__(self, event: InboundEvent) -> object:
        self.calls.append(event)
        if event.event_id in self.raise_on:
            raise RuntimeError(f"dispatch error for {event.event_id}")
        return self.results.get(event.event_id, type("R", (), {"ok": True})())


class FakeSleeper:
    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float):
        self.sleeps.append(seconds)


# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------

def test_run_once_with_zero_items(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    fake_dispatch = FakeDispatch()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    count = worker.run_once()
    assert count == 0
    assert len(fake_dispatch.calls) == 0


def test_worker_clamps_batch_size_and_interval(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")

    worker = GatewayInboxWorker(
        store=store,
        dispatch=FakeDispatch(),
        batch_size=0,
        interval_seconds=0,
        sleeper=FakeSleeper(),
    )

    assert worker.batch_size == 1
    assert worker.interval_seconds == 0.1


def test_run_once_with_one_item_success(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    item_id = _enqueue(store, event_id="evt-1", text="hello")
    fake_dispatch = FakeDispatch()
    sleeper = FakeSleeper()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=sleeper,
    )
    count = worker.run_once()
    assert count == 1
    assert len(fake_dispatch.calls) == 1
    assert fake_dispatch.calls[0].event_id == "evt-1"
    item = store.get(item_id)
    assert item.status == "succeeded"


def test_run_once_with_one_item_failure(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    item_id = _enqueue(store, event_id="evt-1", text="hello")
    fake_dispatch = FakeDispatch()
    fake_dispatch.raise_on.add("evt-1")
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    count = worker.run_once()
    assert count == 1
    assert len(fake_dispatch.calls) == 1
    item = store.get(item_id)
    assert item is not None
    assert item.status == "failed"


def test_run_once_treats_failed_dispatch_result_as_retryable(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    item_id = _enqueue(store, event_id="evt-1", text="hello")
    fake_dispatch = FakeDispatch()
    fake_dispatch.results["evt-1"] = type("R", (), {"ok": False, "error": "agent failed"})()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )

    count = worker.run_once()

    assert count == 1
    item = store.get(item_id)
    assert item is not None
    assert item.status == "failed"
    assert item.last_error == "agent failed"


def test_run_once_with_multiple_items(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    id1 = _enqueue(store, event_id="evt-1", text="msg1")
    id2 = _enqueue(store, event_id="evt-2", text="msg2")
    id3 = _enqueue(store, event_id="evt-3", text="msg3")
    fake_dispatch = FakeDispatch()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    count = worker.run_once()
    assert count == 3
    assert [c.event_id for c in fake_dispatch.calls] == ["evt-1", "evt-2", "evt-3"]
    assert store.get(id1).status == "succeeded"
    assert store.get(id2).status == "succeeded"
    assert store.get(id3).status == "succeeded"


def test_run_once_with_multiple_items_one_fails(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    id1 = _enqueue(store, event_id="evt-1", text="msg1")
    id2 = _enqueue(store, event_id="evt-2", text="msg2")
    id3 = _enqueue(store, event_id="evt-3", text="msg3")
    fake_dispatch = FakeDispatch()
    fake_dispatch.raise_on.add("evt-2")
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    count = worker.run_once()
    assert count == 3
    assert store.get(id1).status == "succeeded"
    assert store.get(id2).status == "failed"
    assert store.get(id3).status == "succeeded"


def test_claim_due_limit_respected(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    for i in range(7):
        _enqueue(store, event_id=f"evt-{i}", text=f"msg{i}")
    fake_dispatch = FakeDispatch()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=3,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    count = worker.run_once()
    assert count == 3
    assert len(fake_dispatch.calls) == 3
    assert [c.event_id for c in fake_dispatch.calls] == ["evt-0", "evt-1", "evt-2"]


def test_event_from_row_reconstructs_inbound_event(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    item_id = _enqueue(
        store,
        platform="feishu",
        event_id="evt-x",
        event_type="message",
        chat_id="oc_abc",
        text="hello world",
        thread_id="th_1",
        sender_id="ou_1",
        sender_name="Miku",
        raw={"extra": "field"},
    )
    row = store.get(item_id)
    assert row is not None

    event = GatewayInboxWorker._event_from_row(row)
    assert isinstance(event, InboundEvent)
    assert event.platform == "feishu"
    assert event.event_id == "evt-x"
    assert event.event_type == "message"
    assert event.chat_id == "oc_abc"
    assert event.text == "hello world"
    assert event.thread_id == "th_1"
    assert event.sender_id == "ou_1"
    assert event.sender_name == "Miku"
    assert event.raw == {"extra": "field"}
    assert event.timestamp is not None


def test_run_forever_calls_recover_stale_processing(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(store, event_id="evt-1", text="msg1")

    recovered: list[int] = []
    original_recover = store.recover_stale_processing

    def tracking_recover(**kwargs):
        recovered.append(1)
        return original_recover(**kwargs)

    store.recover_stale_processing = tracking_recover  # type: ignore

    fake_dispatch = FakeDispatch()
    sleeper = FakeSleeper()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=0.01,
        sleeper=sleeper,
    )

    def raise_after_one():
        raise KeyboardInterrupt("stop")

    import unittest.mock as mock
    with mock.patch.object(worker, "run_once", side_effect=raise_after_one):
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            pass

    assert len(recovered) == 1


def test_request_stop_stops_loop(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    for i in range(10):
        _enqueue(store, event_id=f"evt-{i}", text=f"msg{i}")

    fake_dispatch = FakeDispatch()
    sleeper = FakeSleeper()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=3,
        interval_seconds=0.01,
        sleeper=sleeper,
    )

    import unittest.mock as mock
    stop_called = []

    original_run_once = worker.run_once

    def counting_run_once():
        result = original_run_once()
        if len(fake_dispatch.calls) >= 3 and not stop_called:
            stop_called.append(True)
            worker.request_stop()
        return result

    worker.run_once = counting_run_once
    worker.run_forever()

    assert len(fake_dispatch.calls) == 3


def test_retry_delays_passed_to_fail(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(store, event_id="evt-1", text="hello")

    custom_delays = [1, 3, 5]
    fake_dispatch = FakeDispatch()
    fake_dispatch.raise_on.add("evt-1")

    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        retry_delays=custom_delays,
        sleeper=FakeSleeper(),
    )

    import unittest.mock as mock
    with mock.patch.object(store, "fail", wraps=store.fail) as wrapped_fail:
        worker.run_once()
        wrapped_fail.assert_called_once()
        _, kwargs = wrapped_fail.call_args
        assert kwargs["retry_delays"] == custom_delays


def test_default_retry_delays_when_none_provided(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(store, event_id="evt-1", text="hello")

    fake_dispatch = FakeDispatch()
    fake_dispatch.raise_on.add("evt-1")

    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        retry_delays=None,
        sleeper=FakeSleeper(),
    )

    import unittest.mock as mock
    with mock.patch.object(store, "fail", wraps=store.fail) as wrapped_fail:
        worker.run_once()
        wrapped_fail.assert_called_once()
        _, kwargs = wrapped_fail.call_args
        assert kwargs["retry_delays"] is None


def test_dispatch_called_with_correct_inbound_event(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(
        store,
        platform="feishu",
        event_id="evt-correct",
        event_type="im.message.receive_v1",
        chat_id="oc_correct",
        text="correct text",
        thread_id="th_correct",
        sender_id="ou_correct",
        sender_name="Correct User",
        raw={"foo": "bar"},
    )

    fake_dispatch = FakeDispatch()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=1.0,
        sleeper=FakeSleeper(),
    )
    worker.run_once()

    assert len(fake_dispatch.calls) == 1
    event = fake_dispatch.calls[0]
    assert event.platform == "feishu"
    assert event.event_id == "evt-correct"
    assert event.event_type == "im.message.receive_v1"
    assert event.chat_id == "oc_correct"
    assert event.text == "correct text"
    assert event.thread_id == "th_correct"
    assert event.sender_id == "ou_correct"
    assert event.sender_name == "Correct User"
    assert event.raw == {"foo": "bar"}
    assert event.timestamp is not None


def test_run_forever_sleeps_between_iterations(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(store, event_id="evt-1", text="msg1")

    fake_dispatch = FakeDispatch()
    sleeper = FakeSleeper()

    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=0.05,
        sleeper=sleeper,
    )

    import unittest.mock as mock
    call_count = [0]

    def stop_after_first():
        call_count[0] += 1
        if call_count[0] > 1:
            worker.request_stop()
        return 0

    with mock.patch.object(worker, "run_once", side_effect=stop_after_first):
        worker.run_forever()

    assert call_count[0] == 2
    assert len(sleeper.sleeps) == 1
    assert sleeper.sleeps[0] == 0.1


def test_worker_stops_on_keyboard_interrupt(tmp_path):
    store = GatewayInboxStore(tmp_path / "inbox.sqlite")
    _enqueue(store, event_id="evt-1", text="msg1")

    fake_dispatch = FakeDispatch()
    sleeper = FakeSleeper()
    worker = GatewayInboxWorker(
        store=store,
        dispatch=fake_dispatch,
        batch_size=5,
        interval_seconds=0.01,
        sleeper=sleeper,
    )

    import unittest.mock as mock
    with mock.patch.object(worker, "run_once", side_effect=KeyboardInterrupt):
        worker.run_forever()
