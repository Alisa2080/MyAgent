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


def test_build_agent_recovers_terminal_processes_after_loading_memory(monkeypatch):
    import agent_core.builders as builders

    calls = []

    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: calls.append("load"))
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda namespace: "")
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

    assert calls == ["load", "recover"]
    assert result["agent"]["system_prompt"] == "prompt"
