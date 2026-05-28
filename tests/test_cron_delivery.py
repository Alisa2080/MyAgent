from __future__ import annotations

from dataclasses import dataclass


def test_enqueue_local_result_marks_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "local"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "local"
    assert stored["status"] == "delivered"


def test_silent_success_does_not_enqueue(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=True, output_doc="# out", final_response="[SILENT] nothing"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is None
    assert DeliveryStore().stats()["pending"] == 0


def test_webhook_success(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert sent[0][1]["job_id"] == "job-1"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_webhook_4xx_marks_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    def fake_post(url, payload, timeout=10):
        return 401, "unauthorized"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["dead"] == 1
    stored = DeliveryStore().get(event["id"])
    assert stored["status"] == "dead"
    assert "HTTP 401" in stored["last_error"]
