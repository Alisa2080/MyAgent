import contextlib
import json


class FakeDockerEnv:
    def __init__(self):
        self.calls = []
        self.network_events = []
        self.cwd = "/workspace"

    @contextlib.contextmanager
    def temporary_network(self):
        self.network_events.append("connect")
        try:
            yield
        finally:
            self.network_events.append("disconnect")

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": "ok\n", "returncode": 0}


def test_terminal_tool_wraps_foreground_command_in_network_context(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    env = FakeDockerEnv()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "cwd": "/workspace",
            "timeout": 180,
            "runtime_profile": "hosted",
        },
    )
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type: {"approved": True})

    raw = terminal_tool.terminal_tool(
        command="curl https://example.com",
        task_id="task-1",
        allow_network_once=True,
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert env.network_events == ["connect", "disconnect"]


def test_terminal_tool_cleans_vm_when_network_disconnect_fails(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class BrokenEnv(FakeDockerEnv):
        @contextlib.contextmanager
        def temporary_network(self):
            self.network_events.append("connect")
            yield
            raise RuntimeError("disconnect failed")

    env = BrokenEnv()
    cleaned = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "cwd": "/workspace",
            "timeout": 180,
            "runtime_profile": "hosted",
        },
    )
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type: {"approved": True})
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    raw = terminal_tool.terminal_tool(
        command="curl https://example.com",
        task_id="task-1",
        allow_network_once=True,
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert "network_warning" in payload
    assert cleaned == ["task-1"]


def test_terminal_tool_allows_local_network_approval_without_network_context(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class FakeLocalEnv:
        cwd = "/workspace"

        def __init__(self):
            self.calls = []
            self.network_events = []

        def execute(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {"output": "ok\n", "returncode": 0}

    env = FakeLocalEnv()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "cwd": "/workspace",
            "timeout": 180,
            "runtime_profile": "dev",
        },
    )
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type: {"approved": True})

    raw = terminal_tool.terminal_tool(
        command="curl https://example.com",
        task_id="task-1",
        allow_network_once=True,
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert env.network_events == []


def test_process_registry_releases_background_network_lease(monkeypatch):
    from agent_tools.hermes_terminal_toolkit.process_registry import ProcessRegistry

    released = []
    registry = ProcessRegistry()
    session = registry.spawn_via_env(
        env=FakeDockerEnv(),
        command="python server.py",
        cwd="/workspace",
        task_id="task-bg",
        session_key="",
        network_release=lambda: released.append("released"),
    )

    registry._move_to_finished(session)

    assert released == ["released"]


def test_process_registry_releases_background_network_lease_after_spawn_failure():
    from agent_tools.hermes_terminal_toolkit.process_registry import ProcessRegistry

    class FailingDockerEnv(FakeDockerEnv):
        def execute(self, command, **kwargs):
            raise RuntimeError("spawn failed")

    released = []
    registry = ProcessRegistry()

    session = registry.spawn_via_env(
        env=FailingDockerEnv(),
        command="python server.py",
        cwd="/workspace",
        task_id="task-bg",
        session_key="",
        network_release=lambda: released.append("released"),
    )

    assert session.exited is True
    assert released == ["released"]
