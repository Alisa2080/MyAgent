from __future__ import annotations


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
    assert stored["adapter_key"] == "local"
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


def test_failed_origin_result_enqueues_error_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=False, output_doc="# out", final_response="", error="boom"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "origin"
    assert stored["status"] == "pending"
    assert stored["last_error"] is None
    assert '"status": "error"' in stored["payload_json"]
    assert '"error": "boom"' in stored["payload_json"]


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


def test_invalid_webhook_url_is_dead_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:file:///etc/passwd"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    stored = DeliveryStore().get(event["id"])
    assert stored["status"] == "dead"
    assert "http or https" in stored["last_error"]


def test_webhook_sender_rejects_dns_private_address(monkeypatch):
    import socket

    from cron.delivery import default_webhook_sender

    def fake_getaddrinfo(host, port, type=0):
        assert host == "internal.example"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    try:
        default_webhook_sender("https://internal.example/hook", {"ok": True})
    except ValueError as exc:
        assert "private or local" in str(exc)
    else:
        raise AssertionError("expected private DNS target to be rejected")


def test_webhook_sender_does_not_follow_redirects(monkeypatch):
    import urllib.error
    import urllib.request

    from cron.delivery import default_webhook_sender

    opened = {}

    class FakeOpener:
        def open(self, request, timeout=10):
            opened["url"] = request.full_url
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "http://127.0.0.1/admin"},
                None,
            )

    monkeypatch.setenv("AGENT_CRON_ALLOW_PRIVATE_WEBHOOKS", "1")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())

    status, _body = default_webhook_sender("https://example.invalid/hook", {"ok": True})

    assert status == 302
    assert opened["url"] == "https://example.invalid/hook"


def test_process_due_does_not_claim_origin_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    summary = process_due(limit=10)
    stored = DeliveryStore().get(event["id"])
    assert summary["claimed"] == 0
    assert stored["status"] == "pending"
    assert stored["attempt_count"] == 0


def test_enqueue_result_creates_event_per_delivery_target(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    events = enqueue_result(
        {
            "id": "job-1",
            "name": "Daily",
            "deliver": "origin,webhook:https://example.invalid/hook,local",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert isinstance(events, list)
    assert len(events) == 3
    stored = [DeliveryStore().get(event["id"]) for event in events]
    assert [(event["target_type"], event["adapter_key"], event["address"]) for event in stored] == [
        ("origin", "origin", "session-1"),
        ("webhook", "webhook", "https://example.invalid/hook"),
        ("local", "local", None),
    ]
