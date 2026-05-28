from __future__ import annotations

from datetime import timedelta


def test_enqueue_and_claim_due_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path="/tmp/out.md",
        payload={"type": "cron_result", "status": "ok"},
    )

    claimed = store.claim_due(limit=10)

    assert len(claimed) == 1
    assert claimed[0]["id"] == event["id"]
    assert claimed[0]["status"] == "delivering"
    assert claimed[0]["attempt_count"] == 1


def test_mark_failed_then_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore

    store = DeliveryStore(max_attempts=2)
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )

    first = store.claim_due(limit=1)[0]
    store.mark_failed(first["id"], "timeout")
    failed = store.get(event["id"])
    assert failed["status"] == "failed"
    assert failed["last_error"] == "timeout"
    assert failed["next_attempt_at"] is not None

    store.update_event(event["id"], status="pending", next_attempt_at=None, attempt_count=2)
    second = store.claim_due(limit=1)[0]
    store.mark_failed(second["id"], "still failing")
    dead = store.get(event["id"])
    assert dead["status"] == "dead"
    assert dead["last_error"] == "still failing"


def test_stats_and_stale_delivering(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore, utc_now

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="origin",
        target_type="origin",
        target_id="thread-1",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )
    store.update_event(
        event["id"],
        status="delivering",
        updated_at=(utc_now() - timedelta(minutes=20)).isoformat(),
    )

    stats = store.stats()
    stale = store.stale_delivering(max_age_seconds=600)

    assert stats["delivering"] == 1
    assert stale[0]["id"] == event["id"]
