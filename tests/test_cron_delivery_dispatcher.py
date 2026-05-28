from __future__ import annotations


def test_dispatcher_delivers_webhook_and_marks_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    registry = default_delivery_registry(webhook_sender=fake_post)
    summary = DeliveryDispatcher(registry=registry).dispatch_due(limit=10)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert sent[0][1]["event_id"] == event["id"]
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_dispatcher_marks_retryable_webhook_failure_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    registry = default_delivery_registry(webhook_sender=lambda url, payload, timeout=10: (500, "down"))
    summary = DeliveryDispatcher(registry=registry).dispatch_due(limit=10)

    stored = DeliveryStore().get(event["id"])
    assert summary["failed"] == 1
    assert stored["status"] == "failed"
    assert stored["attempt_count"] == 1
    assert "HTTP 500" in stored["last_error"]
    assert stored["next_attempt_at"]
