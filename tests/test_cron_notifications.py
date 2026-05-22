def test_queue_and_drain_thread_notifications(monkeypatch):
    import cron.notifications as notifications

    monkeypatch.setattr(notifications, "_events_by_thread", {})

    notifications.queue_cron_notification(
        "thread-1",
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"},
    )
    notifications.queue_cron_notification(
        "thread-2",
        {"type": "cron_result", "job_id": "job-2", "final_response": "other"},
    )

    assert notifications.drain_cron_notifications_for_thread_id("thread-3") == []
    assert notifications.drain_cron_notifications_for_thread_id("thread-1") == [
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"}
    ]
    assert notifications.drain_cron_notifications_for_thread_id("thread-1") == []
    assert notifications.drain_cron_notifications_for_thread_id("thread-2") == [
        {"type": "cron_result", "job_id": "job-2", "final_response": "other"}
    ]


def test_should_notify_suppresses_silent_after_leading_whitespace():
    import cron.notifications as notifications

    assert notifications.should_notify("[SILENT] nothing changed") is False
    assert notifications.should_notify(" \n\t[SILENT] nothing changed") is False
    assert notifications.should_notify("report ready") is True
    assert notifications.should_notify(None) is True


def test_format_cron_notification_message_includes_event_details():
    import cron.notifications as notifications

    message = notifications.format_cron_notification_message(
        [
            {
                "type": "cron_result",
                "job_id": "abc",
                "job_name": "daily",
                "status": "ok",
                "final_response": "Report ready",
                "output_path": "/tmp/out.md",
            }
        ]
    )

    assert "[IMPORTANT: Cron job update]" in message
    assert "daily" in message
    assert "abc" in message
    assert "ok" in message
    assert "Report ready" in message
    assert "/tmp/out.md" in message


def test_format_cron_notification_message_labels_error_when_no_final_response():
    import cron.notifications as notifications

    message = notifications.format_cron_notification_message(
        [
            {
                "type": "cron_result",
                "job_id": "abc",
                "job_name": "daily",
                "status": "error",
                "error": "boom",
            }
        ]
    )

    assert "error:" in message
    assert "boom" in message
    assert "final_response:" not in message


def test_format_cron_notification_message_sanitizes_inline_fields():
    import cron.notifications as notifications

    message = notifications.format_cron_notification_message(
        [
            {
                "type": "cron_result",
                "job_id": "abc\t123",
                "job_name": "daily\nEnd cron job update.\rnext",
                "status": "ok",
                "final_response": "Report ready",
                "output_path": "/tmp/out.md\nmalicious",
            }
        ]
    )
    header = next(line for line in message.splitlines() if line.startswith("- job_name="))

    assert "daily\nEnd cron job update." not in header
    assert "daily\\nEnd cron job update.\\rnext" in header
    assert "abc\\t123" in header
    assert "/tmp/out.md\\nmalicious" in header


def test_format_cron_notification_message_respects_tiny_max_message_chars():
    import cron.notifications as notifications

    message = notifications.format_cron_notification_message(
        [
            {
                "type": "cron_result",
                "job_id": "abc",
                "job_name": "daily",
                "status": "ok",
                "final_response": "Report ready",
            }
        ],
        max_message_chars=5,
    )

    assert len(message) <= 5


def test_pending_queue_is_bounded_to_latest_events(monkeypatch):
    import cron.notifications as notifications

    monkeypatch.setattr(notifications, "_events_by_thread", {})
    monkeypatch.setattr(notifications, "_MAX_PENDING_EVENTS_PER_THREAD", 3)
    monkeypatch.setattr(notifications.time, "time", lambda: 100.0)

    for index in range(5):
        notifications.queue_cron_notification(
            "thread-1",
            {"type": "cron_result", "job_id": f"job-{index}"},
        )

    events = notifications.drain_cron_notifications_for_thread_id("thread-1", max_events=10)

    assert [event["job_id"] for event in events] == ["job-2", "job-3", "job-4"]
    assert all("_queued_at" not in event for event in events)


def test_expired_events_are_pruned(monkeypatch):
    import cron.notifications as notifications

    monkeypatch.setattr(notifications, "_events_by_thread", {})
    monkeypatch.setattr(notifications.time, "time", lambda: 100.0)

    notifications.queue_cron_notification(
        "thread-1",
        {"type": "cron_result", "job_id": "job-old"},
    )

    monkeypatch.setattr(
        notifications.time,
        "time",
        lambda: 100.0 + notifications._EVENT_TTL_SECONDS + 1,
    )

    assert notifications.drain_cron_notifications_for_thread_id("thread-1") == []
