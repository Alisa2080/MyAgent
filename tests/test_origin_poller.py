"""Tests for origin polling and poll state persistence."""
from __future__ import annotations

import pytest


def test_origin_poller_persists_poll_state_between_runs(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore
    import os

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()

    class MockPoller:
        key = "test"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            if cursor is None:
                return {
                    "events": [
                        {"id": "e1", "type": "test", "created_at": "2026-05-28T10:00:00Z"},
                        {"id": "e2", "type": "test", "created_at": "2026-05-28T11:00:00Z"},
                    ],
                    "next_cursor": "cursor-1",
                }
            return {"events": [], "next_cursor": None}

    poller = MockPoller()

    result1 = origin_poller.poll_deliveries([poller], store=store, limit=100)
    assert result1["polled"] == 1
    assert result1["collected"] == 2

    result2 = origin_poller.poll_deliveries([poller], store=store, limit=100)
    assert result2["polled"] == 1
    assert result2["collected"] == 0

    db_path = tmp_path / "cron" / "cron.sqlite3"
    assert db_path.exists()


def test_origin_poller_uses_existing_cursor_on_subsequent_polls(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()

    cursors_seen = []

    class MockPoller:
        key = "test-cursor"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            cursors_seen.append(cursor)
            if cursor is None:
                return {
                    "events": [{"id": "e1", "type": "test", "created_at": "2026-05-28T10:00:00Z"}],
                    "next_cursor": "saved-cursor-123",
                }
            return {"events": [], "next_cursor": cursor}

    poller = MockPoller()

    origin_poller.poll_deliveries([poller], store=store, limit=100)
    assert cursors_seen == [None]

    origin_poller.poll_deliveries([poller], store=store, limit=100)
    assert cursors_seen == [None, "saved-cursor-123"]

    origin_poller.poll_deliveries([poller], store=store, limit=100)
    assert cursors_seen == [None, "saved-cursor-123", "saved-cursor-123"]


def test_origin_poller_enqueues_events_to_store(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()

    class MockPoller:
        key = "enqueue-test"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            return {
                "events": [
                    {"id": "evt1", "type": "message", "content": "hello"},
                    {"id": "evt2", "type": "message", "content": "world"},
                ],
                "next_cursor": "page-2",
            }

    poller = MockPoller()
    result = origin_poller.poll_deliveries([poller], store=store, limit=100)

    assert result["polled"] == 1
    assert result["collected"] == 2

    pending = store.stats()["pending"]
    assert pending == 2


def test_origin_poller_handles_no_events(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()

    class MockPoller:
        key = "empty-test"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            return {"events": [], "next_cursor": None}

    poller = MockPoller()
    result = origin_poller.poll_deliveries([poller], store=store, limit=100)

    assert result["polled"] == 1
    assert result["collected"] == 0

    stats = store.stats()
    assert stats["pending"] == 0


def test_origin_poller_multiple_pollers(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()

    class PollerA:
        key = "poller-a"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            return {
                "events": [{"id": "a1", "type": "a"}],
                "next_cursor": "a-cursor",
            }

    class PollerB:
        key = "poller-b"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            return {
                "events": [{"id": "b1", "type": "b"}, {"id": "b2", "type": "b"}],
                "next_cursor": "b-cursor",
            }

    result = origin_poller.poll_deliveries([PollerA(), PollerB()], store=store, limit=100)

    assert result["polled"] == 2
    assert result["collected"] == 3
    assert store.stats()["pending"] == 3
