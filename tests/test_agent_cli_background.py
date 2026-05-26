from pathlib import Path
from threading import Event

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


def test_background_registry_consumes_queued_steer(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data["messages"][0]["content"])
        if len(calls) == 1:
            store.add_steer("bg_12345678", "second instruction")
        return {"messages": [{"role": "assistant", "content": f"turn {len(calls)}"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )
    registry.new_task_id = lambda: "bg_12345678"

    record = registry.start("first instruction")
    registry.join(record.task_id, timeout=2)

    assert calls == ["first instruction", "second instruction"]
    assert store.get_task(record.task_id).status == "completed"


def test_background_registry_stop_requests_interrupt_and_stops_after_turn(
    tmp_path: Path,
):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    interrupts = []

    def runner(input_data, config):
        registry.stop("bg_12345678")
        return {"messages": [{"role": "assistant", "content": "done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
        stop_wait_interrupt=lambda session_id: interrupts.append(session_id),
    )
    registry.new_task_id = lambda: "bg_12345678"

    record = registry.start("first instruction")
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "stopped"
    assert interrupts == ["session-1"]


def test_background_registry_waits_for_approval_and_resumes(tmp_path: Path):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data)
        if len(calls) == 1:
            return {
                "__interrupt__": [
                    {
                        "value": {
                            "action_requests": [
                                {"name": "terminal", "args": {"command": "pwd"}}
                            ],
                            "review_configs": [{"description": "review"}],
                        }
                    }
                ]
            }
        assert isinstance(input_data, Command)
        assert input_data.resume == {"decisions": [{"type": "approve"}]}
        return {"messages": [{"role": "assistant", "content": "approved done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    assert store.get_task(record.task_id).status == "waiting_approval"

    approval_requests = registry.approval_requests(record.task_id)
    assert approval_requests[0].tool_name == "terminal"

    registry.approve(record.task_id, {"decisions": [{"type": "approve"}]})
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "completed"
    assert store.get_task(record.task_id).last_result_preview == "approved done"


def test_background_registry_allows_none_approval_resume(tmp_path: Path):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data)
        if len(calls) == 1:
            return {
                "__interrupt__": [
                    {
                        "value": {
                            "action_requests": [
                                {"name": "terminal", "args": {"command": "pwd"}}
                            ],
                            "review_configs": [{"description": "review"}],
                        }
                    }
                ]
            }
        assert isinstance(input_data, Command)
        assert input_data.resume is None
        return {"messages": [{"role": "assistant", "content": "approved none"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    registry.approve(record.task_id, None)
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "completed"
    assert store.get_task(record.task_id).last_result_preview == "approved none"


def test_background_registry_stop_during_approval_resume_stops_after_turn(
    tmp_path: Path,
):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    resume_started = Event()
    allow_resume = Event()

    def runner(input_data, config):
        if isinstance(input_data, Command):
            resume_started.set()
            allow_resume.wait(2)
            return {"messages": [{"role": "assistant", "content": "resumed done"}]}
        return {
            "__interrupt__": [
                {
                    "value": {
                        "action_requests": [
                            {"name": "terminal", "args": {"command": "pwd"}}
                        ],
                        "review_configs": [{"description": "review"}],
                    }
                }
            ]
        }

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    registry.approve(record.task_id, {"decisions": [{"type": "approve"}]})
    assert resume_started.wait(2)

    registry.stop(record.task_id)
    allow_resume.set()
    registry.join(record.task_id, timeout=2)

    stopped = store.get_task(record.task_id)
    assert stopped.status == "stopped"
    assert stopped.last_result_preview is None


def test_background_registry_rejects_duplicate_approval(tmp_path: Path):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    resume_started = Event()
    allow_resume = Event()

    def runner(input_data, config):
        if isinstance(input_data, Command):
            resume_started.set()
            allow_resume.wait(2)
            return {"messages": [{"role": "assistant", "content": "approved done"}]}
        return {
            "__interrupt__": [
                {
                    "value": {
                        "action_requests": [
                            {"name": "terminal", "args": {"command": "pwd"}}
                        ],
                        "review_configs": [{"description": "review"}],
                    }
                }
            ]
        }

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    registry.approve(record.task_id, {"decisions": [{"type": "approve"}]})

    try:
        registry.approve(record.task_id, {"decisions": [{"type": "reject"}]})
    except ValueError as exc:
        assert "already submitted" in str(exc)
    else:
        raise AssertionError("duplicate approval should fail")

    assert resume_started.wait(2)
    allow_resume.set()
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).last_result_preview == "approved done"


def test_background_registry_rejects_stale_approval_requests_after_completion(
    tmp_path: Path,
):
    from langgraph.types import Command

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    def runner(input_data, config):
        if isinstance(input_data, Command):
            return {"messages": [{"role": "assistant", "content": "approved done"}]}
        return {
            "__interrupt__": [
                {
                    "value": {
                        "action_requests": [
                            {"name": "terminal", "args": {"command": "pwd"}}
                        ],
                        "review_configs": [{"description": "review"}],
                    }
                }
            ]
        }

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )

    record = registry.start("needs approval")
    registry.wait_for_status(record.task_id, "waiting_approval", timeout=2)
    registry.approve(record.task_id, {"decisions": [{"type": "approve"}]})
    registry.join(record.task_id, timeout=2)

    assert store.get_task(record.task_id).status == "completed"
    try:
        registry.approval_requests(record.task_id)
    except ValueError as exc:
        assert "not waiting for approval" in str(exc)
    else:
        raise AssertionError("stale approval requests should fail")


def test_background_registry_rejects_steer_while_stopping(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.request_stop("bg_12345678")
    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=lambda input_data, config: {},
    )

    try:
        registry.steer("bg_12345678", "try this next")
    except ValueError as exc:
        assert "stopping" in str(exc)
    else:
        raise AssertionError("steer while stopping should fail")

    assert store.get_task("bg_12345678").pending_steer_count == 0


def test_background_store_rejects_steer_while_stopping(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.request_stop("bg_12345678")

    try:
        store.add_steer("bg_12345678", "try this next")
    except ValueError as exc:
        assert "stopping" in str(exc)
    else:
        raise AssertionError("store steer while stopping should fail")

    assert store.get_task("bg_12345678").pending_steer_count == 0


def test_background_store_does_not_complete_when_pending_steers_exist(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.mark_started("bg_12345678")
    store.add_steer("bg_12345678", "try this next")

    assert store.mark_completing_if_idle("bg_12345678") is None
    record = store.get_task("bg_12345678")
    assert record.status == "running"
    assert record.pending_steer_count == 1


def test_background_store_rejects_steer_while_completing(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.mark_started("bg_12345678")
    completing = store.mark_completing_if_idle("bg_12345678")

    assert completing.status == "completing"
    try:
        store.add_steer("bg_12345678", "too late")
    except ValueError as exc:
        assert "completing" in str(exc)
    else:
        raise AssertionError("steer while completing should fail")


def test_background_registry_finalize_requires_cancel_requested(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.mark_started("bg_12345678")
    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=lambda input_data, config: {},
    )

    record = registry.finalize_stopping("bg_12345678")

    assert record.status == "running"


def test_background_store_does_not_stop_completing_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.mark_started("bg_12345678")
    completing = store.mark_completing_if_idle("bg_12345678")

    stopped = store.request_stop("bg_12345678")

    assert completing.status == "completing"
    assert stopped.status == "completing"
    assert stopped.cancel_requested is False


def test_background_store_does_not_complete_stopping_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.mark_started("bg_12345678")
    stopping = store.request_stop("bg_12345678")

    completed = store.mark_completed("bg_12345678", result_preview="done")

    assert stopping.status == "stopping"
    assert completed.status == "stopping"
    assert completed.last_result_preview is None


def test_background_store_does_not_start_stopping_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Fix tests",
        prompt_preview="Fix tests please",
    )
    store.request_stop("bg_12345678")

    started = store.mark_started("bg_12345678")

    assert started.status == "stopping"
    assert started.cancel_requested is True


def test_background_registry_stop_before_worker_turn_skips_runner(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    calls = []

    def runner(input_data, config):
        calls.append(input_data)
        return {"messages": [{"role": "assistant", "content": "done"}]}

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )
    store.create_task(
        task_id="bg_12345678",
        session_id="session-1",
        title="Task title",
        prompt_preview="first instruction",
    )
    store.request_stop("bg_12345678")

    registry._worker("bg_12345678", "session-1", "first instruction")

    assert calls == []
    assert store.get_task("bg_12345678").status == "stopped"


def test_background_registry_exception_preserves_cancelled_task(tmp_path: Path):
    store = BackgroundTaskStore(tmp_path / "cli.sqlite")

    def runner(input_data, config):
        registry.stop("bg_12345678")
        raise RuntimeError("boom")

    registry = BackgroundTaskRegistry(
        store=store,
        session_id_factory=lambda: "session-1",
        title_factory=lambda prompt: "Task title",
        runner=runner,
    )
    registry.new_task_id = lambda: "bg_12345678"

    record = registry.start("first instruction")
    registry.join(record.task_id, timeout=2)

    final = store.get_task(record.task_id)
    assert final.status == "stopped"
    assert final.last_error is None
