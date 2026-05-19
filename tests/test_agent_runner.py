import contextlib
from queue import Queue

import pytest


def test_invoke_agent_with_terminal_notifications_runs_initial_turn_only_without_events(
    monkeypatch,
):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    monkeypatch.setattr(
        runner,
        "drain_terminal_notifications_for_thread_id",
        lambda thread_id: [],
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert calls == [
        (
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
        )
    ]


def test_invoke_agent_with_terminal_notifications_resumes_same_thread(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []
    drain_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": [f"turn-{len(calls)}"]}

    def fake_drain(thread_id):
        drain_calls.append(thread_id)
        if len(drain_calls) == 1:
            return [
                {
                    "type": "completion",
                    "session_id": "proc_1",
                    "command": "python job.py",
                    "exit_code": 0,
                }
            ]
        return []

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(
        runner,
        "format_terminal_notification_message",
        lambda events: "formatted notification",
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert len(calls) == 2
    assert calls[1][0] == {
        "messages": [{"role": "user", "content": "formatted notification"}]
    }
    assert calls[1][1] == {"configurable": {"thread_id": "thread-1"}}
    assert drain_calls == ["thread-1", "thread-1"]


def test_invoke_agent_with_terminal_notifications_resumes_from_real_terminal_queue(
    monkeypatch,
):
    import agent_core.agent_runner as runner
    import agent_core.terminal_notifications as notifications
    from agent_core.session_context import hermes_task_id_from_thread_id

    completion_queue = Queue()
    completion_queue.put(
        {
            "type": "completion",
            "task_id": hermes_task_id_from_thread_id("thread-1"),
            "session_id": "proc_1",
            "command": "python job.py",
            "exit_code": 0,
            "output": "finished successfully",
        }
    )
    monkeypatch.setattr(notifications.process_registry, "completion_queue", completion_queue)
    monkeypatch.setattr(notifications, "_pending_events_by_task", {})

    class FakeAgent:
        def __init__(self):
            self.invokes = []

        def invoke(self, input_data, config=None):
            self.invokes.append((input_data, config))
            return {"messages": [f"turn-{len(self.invokes)}"]}

    agent = FakeAgent()
    config = {"configurable": {"thread_id": "thread-1"}}

    result = runner.invoke_agent_with_terminal_notifications(
        agent,
        {"messages": [{"role": "user", "content": "start"}]},
        config,
    )

    assert result == {"messages": ["turn-2"]}
    assert len(agent.invokes) == 2
    assert agent.invokes[0] == (
        {"messages": [{"role": "user", "content": "start"}]},
        config,
    )
    assert agent.invokes[1][1] == config

    resume_message = agent.invokes[1][0]["messages"][0]
    assert resume_message["role"] == "user"
    assert "[IMPORTANT: Background terminal update]" in resume_message["content"]
    assert "session_id=proc_1" in resume_message["content"]
    assert "finished successfully" in resume_message["content"]
    assert completion_queue.empty()


def test_invoke_agent_with_terminal_notifications_respects_max_auto_resumes(
    monkeypatch,
):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append(input_data)
            return {"messages": [f"turn-{len(calls)}"]}

    monkeypatch.setattr(
        runner,
        "drain_terminal_notifications_for_thread_id",
        lambda thread_id: [
            {"type": "completion", "session_id": "proc_1", "command": "job"}
        ],
    )
    monkeypatch.setattr(
        runner,
        "format_terminal_notification_message",
        lambda events: "formatted notification",
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        max_auto_resumes=2,
    )

    assert result == {"messages": ["turn-3"]}
    assert len(calls) == 3


def test_invoke_agent_with_terminal_notifications_zero_max_auto_resumes_runs_initial_turn_only(
    monkeypatch,
):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    def fail_drain(thread_id):
        raise AssertionError("should not drain queued notifications")

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fail_drain)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        max_auto_resumes=0,
    )

    assert result == {"messages": ["initial"]}
    assert calls == [
        (
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
        )
    ]


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("invalid", 3),
        ("-2", 0),
        ("0", 0),
        ("4", 4),
    ],
)
def test_max_terminal_auto_resumes_parses_env(monkeypatch, raw_value, expected):
    import agent_core.agent_runner as runner

    monkeypatch.setenv(runner.MAX_AUTO_RESUMES_ENV, raw_value)

    assert runner.max_terminal_auto_resumes() == expected


def test_max_terminal_auto_resumes_defaults_when_env_missing(monkeypatch):
    import agent_core.agent_runner as runner

    monkeypatch.delenv(runner.MAX_AUTO_RESUMES_ENV, raising=False)

    assert runner.max_terminal_auto_resumes() == runner.DEFAULT_MAX_AUTO_RESUMES


def test_runner_does_not_drain_or_resume_without_thread_id(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    def fail_drain(thread_id):
        raise AssertionError("should not drain without thread_id")

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fail_drain)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {},
    )

    assert result == {"messages": ["initial"]}
    assert len(calls) == 1


def test_runner_wraps_initial_and_resume_turns_in_terminal_execution_scope(
    monkeypatch,
):
    import agent_core.agent_runner as runner

    scopes = []
    drain_calls = 0

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, input_data, config=None):
            self.calls += 1
            return {"messages": [f"turn-{self.calls}"]}

    @contextlib.contextmanager
    def fake_scope(thread_id):
        scopes.append(("enter", thread_id))
        try:
            yield
        finally:
            scopes.append(("exit", thread_id))

    def fake_drain(thread_id):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            return [{"type": "completion", "session_id": "proc_1", "command": "job"}]
        return []

    monkeypatch.setattr(runner, "terminal_execution_scope", fake_scope)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(
        runner,
        "format_terminal_notification_message",
        lambda events: "formatted notification",
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert scopes == [
        ("enter", "thread-1"),
        ("exit", "thread-1"),
        ("enter", "thread-1"),
        ("exit", "thread-1"),
    ]


def test_runner_cleans_after_initial_turn(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": ["initial"]}

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert cleanup_calls == [("thread-1", "turn_finished")]


def test_runner_cleans_after_each_resume_turn(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []
    drain_calls = 0

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, input_data, config=None):
            self.calls += 1
            return {"messages": [f"turn-{self.calls}"]}

    def fake_drain(thread_id):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            return [{"type": "completion", "session_id": "proc_1", "command": "job"}]
        return []

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["turn-2"]}
    assert cleanup_calls == [
        ("thread-1", "turn_finished"),
        ("thread-1", "terminal_notification_resume_finished"),
    ]


def test_runner_cleans_when_agent_invoke_raises(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            raise RuntimeError("model failed")

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    with pytest.raises(RuntimeError, match="model failed"):
        runner.invoke_agent_with_terminal_notifications(
            FakeAgent(),
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
        )

    assert cleanup_calls == [("thread-1", "turn_finished")]


def test_runner_preserves_agent_error_when_cleanup_raises(monkeypatch):
    import agent_core.agent_runner as runner

    class FakeAgent:
        def invoke(self, input_data, config=None):
            raise RuntimeError("model failed")

    def fail_cleanup(thread_id, reason="turn_finished"):
        raise RuntimeError("cleanup failed")

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "cleanup_task_resources_for_thread_id", fail_cleanup)

    with pytest.raises(RuntimeError, match="model failed"):
        runner.invoke_agent_with_terminal_notifications(
            FakeAgent(),
            {"messages": [{"role": "user", "content": "start"}]},
            {"configurable": {"thread_id": "thread-1"}},
        )


def test_runner_returns_result_when_cleanup_raises(monkeypatch):
    import agent_core.agent_runner as runner

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": ["initial"]}

    def fail_cleanup(thread_id, reason="turn_finished"):
        raise RuntimeError("cleanup failed")

    monkeypatch.delenv(runner.PER_TURN_CLEANUP_ENV, raising=False)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])
    monkeypatch.setattr(runner, "cleanup_task_resources_for_thread_id", fail_cleanup)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}


def test_runner_per_turn_cleanup_can_be_disabled(monkeypatch):
    import agent_core.agent_runner as runner

    cleanup_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            return {"messages": ["initial"]}

    monkeypatch.setenv(runner.PER_TURN_CLEANUP_ENV, "false")
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", lambda thread_id: [])
    monkeypatch.setattr(
        runner,
        "cleanup_task_resources_for_thread_id",
        lambda thread_id, reason="turn_finished": cleanup_calls.append((thread_id, reason)),
    )

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert cleanup_calls == []


def test_runner_does_not_drain_notifications_after_process_shutdown(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append((input_data, config))
            return {"messages": ["initial"]}

    def fail_drain(thread_id):
        raise AssertionError("should not drain notifications after shutdown")

    monkeypatch.setattr(runner, "is_process_shutdown_requested", lambda: True)
    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fail_drain)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["initial"]}
    assert len(calls) == 1


def test_runner_stops_auto_resume_loop_after_process_shutdown(monkeypatch):
    import agent_core.agent_runner as runner

    calls = []
    drain_calls = []

    class FakeAgent:
        def invoke(self, input_data, config=None):
            calls.append(input_data)
            return {"messages": [f"turn-{len(calls)}"]}

    def fake_drain(thread_id):
        drain_calls.append(thread_id)
        return [{"type": "completion", "session_id": f"proc_{len(drain_calls)}", "command": "job"}]

    monkeypatch.setattr(runner, "drain_terminal_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(runner, "format_terminal_notification_message", lambda events: "formatted notification")
    monkeypatch.setattr(runner, "is_process_shutdown_requested", lambda: len(calls) >= 2)

    result = runner.invoke_agent_with_terminal_notifications(
        FakeAgent(),
        {"messages": [{"role": "user", "content": "start"}]},
        {"configurable": {"thread_id": "thread-1"}},
        max_auto_resumes=3,
    )

    assert result == {"messages": ["turn-2"]}
    assert len(calls) == 2
    assert len(drain_calls) == 1
