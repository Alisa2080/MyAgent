from pathlib import Path

from pathlib import Path

from agent_cli.background import BackgroundTaskRegistry, BackgroundTaskStore


def test_background_registry_runs_task_to_completion(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []
    sessions = []

    def runner(input_data, config):
        calls.append((input_data, config))
        return {"messages": [{"role": "assistant", "content": "done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        session_record_creator=lambda session_id, title: sessions.append((session_id, title)),
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("Fix tests")
    registry.join(record.task_id, timeout=2)

    completed = store.get_task(record.task_id)
    assert completed.status == "completed"
    assert completed.last_result_preview == "done"
    assert calls == [
        (
            {"messages": [{"role": "user", "content": "Fix tests"}]},
            {"configurable": {"thread_id": "session-1"}},
        )
    ]
    assert sessions == [("session-1", "Task title")]
    assert registry.drain_notifications()[0].kind == "done"


def test_background_registry_records_failure(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    def runner(input_data, config):
        raise RuntimeError("agent failed")

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("Fix tests")
    registry.join(record.task_id, timeout=2)

    failed = store.get_task(record.task_id)
    assert failed.status == "failed"
    assert "agent failed" in failed.last_error
    assert registry.drain_notifications()[0].kind == "failed"


from agent_cli.background import BackgroundTaskStore


def test_background_store_creates_and_lists_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    record = store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    assert record.task_id == "bg_12345678"
    assert record.session_id == "session-1"
    assert record.status == "queued"
    assert record.pending_steer_count == 0
    assert store.get_task("bg_12345678") == record
    assert [item.task_id for item in store.list_tasks()] == ["bg_12345678"]


def test_background_store_updates_status_result_and_error(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    store.mark_started("bg_12345678")
    running = store.get_task("bg_12345678")
    assert running.status == "running"
    assert running.started_at is not None

    store.mark_completed("bg_12345678", result_preview="All tests pass")
    completed = store.get_task("bg_12345678")
    assert completed.status == "completed"
    assert completed.last_result_preview == "All tests pass"
    assert completed.finished_at is not None

    store.set_status("bg_12345678", "failed", last_error="boom")
    failed = store.get_task("bg_12345678")
    assert failed.status == "failed"
    assert failed.last_error == "boom"


def test_background_store_queues_and_consumes_steer(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    first = store.add_steer("bg_12345678", "focus on pytest")
    second = store.add_steer("bg_12345678", "then update docs")

    assert first.id < second.id
    assert store.get_task("bg_12345678").pending_steer_count == 2

    pending = store.consume_pending_steers("bg_12345678")

    assert [item.message for item in pending] == ["focus on pytest", "then update docs"]
    assert store.get_task("bg_12345678").pending_steer_count == 0
    assert store.consume_pending_steers("bg_12345678") == []


from agent_cli.session_store import SessionStore


def test_background_store_can_share_session_store_database(tmp_path: Path):
    session_store = SessionStore(tmp_path / "cli.sqlite")
    background_store = BackgroundTaskStore(session_store.db_path)

    record = background_store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )

    assert record.task_id == "bg_12345678"
