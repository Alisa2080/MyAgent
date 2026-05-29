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


def test_dispatcher_retryable_failure_updates_job_delivery_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": "2026-05-28T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-28T10:00:00+00:00", limit=1)[0]
    claimed_job = dict(claimed["job"])
    claimed_job["run_id"] = claimed["run"]["id"]
    enqueue_result(
        claimed_job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    registry = default_delivery_registry(webhook_sender=lambda url, payload, timeout=10: (500, "down"))
    summary = DeliveryDispatcher(store=store, registry=registry).dispatch_due(limit=10)

    assert summary["failed"] == 1
    assert "HTTP 500" in store.get_job(job["id"])["last_delivery_error"]
    assert store.get_run(claimed["run"]["id"])["delivery_status"] == "retrying"


def test_dispatcher_marks_unsupported_persisted_target_dead_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.state_store import StateStore

    store = StateStore()
    event = store.enqueue_delivery_event(
        job_id="job-1",
        run_id=None,
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="slack:C123",
        target_type="slack",
        adapter_key="slack",
        address="C123",
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="pending",
    )

    summary = DeliveryDispatcher(store=store).dispatch_due(limit=10)

    stored = store.get_delivery_event(event["id"])
    assert summary["dead"] == 1
    assert stored["status"] == "dead"
    assert stored["attempt_count"] == 0
    assert "unsupported delivery target" in stored["last_error"]


def test_dispatcher_leaves_origin_events_for_origin_poller(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.state_store import StateStore

    store = StateStore()
    event = store.enqueue_delivery_event(
        job_id="job-1",
        run_id=None,
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="origin",
        target_type="origin",
        adapter_key="origin",
        address="session-1",
        thread_id="thread-1",
        origin={"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="pending",
    )

    summary = DeliveryDispatcher(store=store).dispatch_due(limit=10)

    assert summary == {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
    assert store.get_delivery_event(event["id"])["status"] == "pending"


def test_dispatcher_counts_retry_exhaustion_as_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    registry = default_delivery_registry(webhook_sender=lambda url, payload, timeout=10: (500, "down"))
    summary = DeliveryDispatcher(store=StateStore(max_delivery_attempts=1), registry=registry).dispatch_due(limit=10)

    stored = DeliveryStore().get(event["id"])
    assert summary["dead"] == 1
    assert summary["failed"] == 0
    assert stored["status"] == "dead"


def test_dispatcher_marks_adapter_exception_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import AdapterValidation
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import DeliveryRegistry
    from cron.delivery_store import DeliveryStore

    class RaisingWebhookAdapter:
        key = "webhook"

        def validate(self, target, job):
            return AdapterValidation(True)

        def deliver(self, event, job, run):
            raise RuntimeError("adapter exploded")

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    registry = DeliveryRegistry()
    registry.register(RaisingWebhookAdapter())

    summary = DeliveryDispatcher(registry=registry).dispatch_due(limit=10)

    stored = DeliveryStore().get(event["id"])
    assert summary["failed"] == 1
    assert stored["status"] == "failed"
    assert stored["attempt_count"] == 1
    assert "adapter exploded" in stored["last_error"]


def test_job_delivery_error_aggregates_run_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-28T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-28T10:00:00+00:00", limit=1)[0]
    run_id = claimed["run"]["id"]
    dead = store.enqueue_delivery_event(
        job_id=job["id"],
        run_id=run_id,
        job_name=job["name"],
        run_at="2026-05-28T10:00:00+00:00",
        target="webhook:https://example.invalid/bad",
        target_type="webhook",
        adapter_key="webhook",
        address="https://example.invalid/bad",
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="dead",
        last_error="HTTP 401: unauthorized",
    )
    delivered = store.enqueue_delivery_event(
        job_id=job["id"],
        run_id=run_id,
        job_name=job["name"],
        run_at="2026-05-28T10:00:00+00:00",
        target="local",
        target_type="local",
        adapter_key="local",
        address=None,
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"ok": True},
        status="pending",
    )

    dispatcher = DeliveryDispatcher(store=store)
    dispatcher._sync_after_event_update(dead)
    dispatcher._sync_after_event_update(
        store.update_delivery_event(delivered["id"], status="delivered", last_error=None, next_attempt_at=None)
    )

    assert store.get_run(run_id)["delivery_status"] == "failed"
    assert store.get_job(job["id"])["last_delivery_error"] == "HTTP 401: unauthorized"
