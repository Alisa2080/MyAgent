from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_thread_id


def test_recover_terminal_processes_delegates_to_registry(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []

    def fake_recover():
        calls.append("recover")
        return 2

    monkeypatch.setattr(lifecycle, "_recovery_attempted", False)
    monkeypatch.setattr(lifecycle.process_registry, "recover_from_checkpoint", fake_recover)

    assert lifecycle.recover_terminal_processes() == 2
    assert calls == ["recover"]


def test_recover_terminal_processes_runs_once_per_process(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []

    def fake_recover():
        calls.append("recover")
        return 2

    monkeypatch.setattr(lifecycle, "_recovery_attempted", False)
    monkeypatch.setattr(lifecycle.process_registry, "recover_from_checkpoint", fake_recover)

    assert lifecycle.recover_terminal_processes() == 2
    assert lifecycle.recover_terminal_processes() == 0
    assert calls == ["recover"]


def test_recover_terminal_processes_is_thread_safe(monkeypatch):
    import threading
    import time

    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    recovery_started = threading.Event()
    release_recover = threading.Event()

    def fake_recover():
        calls.append("recover")
        recovery_started.set()
        release_recover.wait(timeout=5)
        return 2

    assert hasattr(lifecycle, "_recovery_lock")
    monkeypatch.setattr(lifecycle, "_recovery_attempted", False)
    monkeypatch.setattr(lifecycle.process_registry, "recover_from_checkpoint", fake_recover)

    results = []
    errors = []

    def worker():
        try:
            results.append(lifecycle.recover_terminal_processes())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()

    assert recovery_started.wait(timeout=5)
    time.sleep(0.05)
    assert results == []

    release_recover.set()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert sorted(results) == [0, 2]
    assert calls == ["recover"]


def test_cleanup_terminal_session_for_thread_id_kills_processes_and_cleans_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = hermes_task_id_from_thread_id("cleanup-thread")
    killed = []
    cleaned = []

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda task_id=None: killed.append(task_id) or 3)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    result = lifecycle.cleanup_terminal_session_for_thread_id("cleanup-thread")

    assert result == {"task_id": task_id, "killed_processes": 3, "environment_cleaned": True}
    assert killed == [task_id]
    assert cleaned == [task_id]


def test_cleanup_terminal_session_for_runtime_uses_runtime_thread(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = hermes_task_id_from_thread_id("runtime-cleanup-thread")
    killed = []
    cleaned = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="runtime-cleanup-thread"))

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda task_id=None: killed.append(task_id) or 1)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    result = lifecycle.cleanup_terminal_session_for_runtime(runtime)

    assert result["task_id"] == task_id
    assert result["killed_processes"] == 1
    assert killed == [task_id]
    assert cleaned == [task_id]


def test_end_terminal_session_delegates_to_thread_cleanup(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle,
        "cleanup_terminal_session_for_thread_id",
        lambda thread_id: calls.append(thread_id) or {"task_id": "task-1", "killed_processes": 2},
    )

    result = lifecycle.end_terminal_session("thread-1", reason="user_closed")

    assert result == {
        "task_id": "task-1",
        "killed_processes": 2,
        "cleaned": True,
        "cleanup_reason": "user_closed",
    }
    assert calls == ["thread-1"]


def test_end_terminal_session_requires_thread_id():
    import agent_core.terminal_lifecycle as lifecycle

    result = lifecycle.end_terminal_session(None)

    assert result["cleaned"] is False
    assert result["cleanup_reason"] == "session_closed"
    assert "thread_id is required" in result["error"]


def test_cleanup_task_resources_for_task_id_cleans_non_persistent_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: False)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: calls.append(task_id))

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result == {
        "task_id": "task-1",
        "cleaned": True,
        "persistent": False,
        "cleanup_reason": "turn_finished",
    }
    assert calls == ["task-1"]


def test_cleanup_task_resources_for_task_id_skips_persistent_env(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: True)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: calls.append(task_id))

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result == {
        "task_id": "task-1",
        "cleaned": False,
        "persistent": True,
        "cleanup_reason": "turn_finished",
    }
    assert calls == []


def test_cleanup_task_resources_for_task_id_reports_cleanup_error(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "is_persistent_env", lambda task_id: False)

    def fail_cleanup(task_id):
        raise RuntimeError(f"cannot clean {task_id}")

    monkeypatch.setattr(lifecycle, "cleanup_vm", fail_cleanup)

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result["task_id"] == "task-1"
    assert result["cleaned"] is False
    assert result["persistent"] is False
    assert result["cleanup_reason"] == "turn_finished"
    assert "cannot clean task-1" in result["error"]


def test_cleanup_task_resources_for_task_id_skips_when_persistence_check_fails(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    cleaned = []

    def fail_persistence_check(task_id):
        raise RuntimeError(f"cannot inspect {task_id}")

    monkeypatch.setattr(lifecycle, "is_persistent_env", fail_persistence_check)
    monkeypatch.setattr(lifecycle, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    result = lifecycle.cleanup_task_resources_for_task_id("task-1", reason="turn_finished")

    assert result["task_id"] == "task-1"
    assert result["cleaned"] is False
    assert result["cleanup_reason"] == "turn_finished"
    assert "cannot inspect task-1" in result["error"]
    assert cleaned == []


def test_cleanup_task_resources_for_thread_id_uses_hashed_task_id(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    task_id = hermes_task_id_from_thread_id("thread-cleanup")
    calls = []

    monkeypatch.setattr(
        lifecycle,
        "cleanup_task_resources_for_task_id",
        lambda task_id, reason="turn_finished": calls.append((task_id, reason))
        or {"task_id": task_id, "cleaned": True, "persistent": False, "cleanup_reason": reason},
    )

    result = lifecycle.cleanup_task_resources_for_thread_id("thread-cleanup", reason="resume_finished")

    assert result == {
        "task_id": task_id,
        "cleaned": True,
        "persistent": False,
        "cleanup_reason": "resume_finished",
    }
    assert calls == [(task_id, "resume_finished")]


def test_cleanup_task_resources_for_thread_id_requires_thread_id():
    import agent_core.terminal_lifecycle as lifecycle

    result = lifecycle.cleanup_task_resources_for_thread_id(None)

    assert result["cleaned"] is False
    assert result["cleanup_reason"] == "turn_finished"
    assert "thread_id is required" in result["error"]


def test_interrupt_unknown_thread_is_noop(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_active_execution_threads", {})

    result = lifecycle.interrupt_terminal_wait_for_thread_id("missing-thread")

    assert result == {"interrupted": False, "reason": "new_user_message"}


def test_terminal_execution_scope_registers_and_clears_interrupt(monkeypatch):
    import threading

    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    current_thread_id = threading.current_thread().ident
    with lifecycle.terminal_execution_scope("thread-1"):
        assert lifecycle._active_execution_threads == {"thread-1": {current_thread_id}}
        result = lifecycle.interrupt_terminal_wait_for_thread_id("thread-1", reason="new_message")

    assert result == {"interrupted": True, "reason": "new_message", "python_thread_ids": [current_thread_id]}
    assert lifecycle._active_execution_threads == {}
    assert calls == [(False, current_thread_id), (True, current_thread_id), (False, current_thread_id)]


def test_terminal_execution_scope_preserves_reentrant_broadcast_after_registration(monkeypatch):
    import threading

    import agent_core.terminal_lifecycle as lifecycle

    class BroadcastAfterAddSet(set):
        def add(self, value):
            super().add(value)
            lifecycle.interrupt_all_terminal_waits(reason="received_signal_15")

    states = {}
    current_thread_id = threading.current_thread().ident
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": BroadcastAfterAddSet()})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: states.__setitem__(thread_id, active))

    with lifecycle.terminal_execution_scope("thread-1"):
        assert states[current_thread_id] is True

    assert states[current_thread_id] is False


def test_terminal_execution_scope_ignores_missing_thread_id(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    with lifecycle.terminal_execution_scope(None):
        assert lifecycle._active_execution_threads == {}

    assert calls == []


def test_interrupt_signals_all_active_executions_for_thread(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": {11, 22}})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    result = lifecycle.interrupt_terminal_wait_for_thread_id("thread-1")

    assert result == {"interrupted": True, "reason": "new_user_message", "python_thread_ids": [11, 22]}
    assert calls == [(True, 11), (True, 22)]


def test_snapshot_active_terminal_execution_threads_returns_copy(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": {11, 22}})

    snapshot = lifecycle.snapshot_active_terminal_execution_threads()
    snapshot["thread-1"].add(33)

    assert snapshot == {"thread-1": {11, 22, 33}}
    assert lifecycle._active_execution_threads == {"thread-1": {11, 22}}


def test_interrupt_all_terminal_waits_signals_every_active_python_thread(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {"thread-1": {11, 22}, "thread-2": {33}})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    result = lifecycle.interrupt_all_terminal_waits(reason="received_signal_15")

    assert result == {
        "interrupted": True,
        "reason": "received_signal_15",
        "python_thread_ids": [11, 22, 33],
        "thread_ids": ["thread-1", "thread-2"],
    }
    assert calls == [(True, 11), (True, 22), (True, 33)]


def test_interrupt_all_terminal_waits_is_noop_without_active_threads(monkeypatch):
    import agent_core.terminal_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_active_execution_threads", {})
    monkeypatch.setattr(lifecycle, "set_interrupt", lambda active, thread_id=None: calls.append((active, thread_id)))

    result = lifecycle.interrupt_all_terminal_waits(reason="received_signal_1")

    assert result == {
        "interrupted": False,
        "reason": "received_signal_1",
        "python_thread_ids": [],
        "thread_ids": [],
    }
    assert calls == []


def test_active_execution_lock_allows_signal_handler_reentry():
    import agent_core.terminal_lifecycle as lifecycle

    lifecycle._active_execution_lock.acquire()
    reacquired = False
    try:
        reacquired = lifecycle._active_execution_lock.acquire(blocking=False)
        assert reacquired is True
    finally:
        if reacquired:
            lifecycle._active_execution_lock.release()
        lifecycle._active_execution_lock.release()


def test_interrupt_lock_allows_signal_handler_reentry():
    from agent_tools.hermes_terminal_toolkit import interrupt

    interrupt._lock.acquire()
    reacquired = False
    try:
        reacquired = interrupt._lock.acquire(blocking=False)
        assert reacquired is True
    finally:
        if reacquired:
            interrupt._lock.release()
        interrupt._lock.release()


def test_build_agent_recovers_terminal_processes_after_loading_memory(monkeypatch):
    import agent_core.builders as builders

    calls = []

    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: calls.append("load"))
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda namespace: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: calls.append("install_signals"))
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: calls.append("recover"))
    monkeypatch.setattr(builders, "build_prompt_context", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])
    monkeypatch.setattr(builders, "model_display_name", lambda model: "model")
    monkeypatch.setattr(builders.SystemPromptBuilder, "build_parent", lambda self, context: "prompt")
    monkeypatch.setattr(builders, "build_tool_call_limit_middleware", lambda include_task=True: [])
    monkeypatch.setattr(builders, "SummarizationMiddleware", lambda **kwargs: "summarize")
    monkeypatch.setattr(builders, "TodoListMiddleware", lambda **kwargs: "todos")
    monkeypatch.setattr(builders, "FlexibleHumanInTheLoopMiddleware", lambda **kwargs: "human")
    monkeypatch.setattr(builders, "ToolRetryMiddleware", lambda **kwargs: "tool-retry")
    monkeypatch.setattr(builders, "ModelRetryMiddleware", lambda **kwargs: "model-retry")
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: {"agent": kwargs})

    result = builders.build_agent()

    assert calls == ["install_signals", "load", "recover"]
    assert result["agent"]["system_prompt"] == "prompt"
