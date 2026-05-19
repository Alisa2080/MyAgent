import json


class FakeEnv:
    def __init__(self, cwd="/workspace"):
        self.cwd = cwd
        self.execute_calls = []
        self.cleaned = False

    def execute(self, command, **kwargs):
        self.execute_calls.append((command, kwargs))
        return {"output": "ok\n", "returncode": 0}

    def cleanup(self):
        self.cleaned = True


def _reset_terminal_env_state(monkeypatch, terminal_tool):
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_cleanup_thread", None)
    monkeypatch.setattr(terminal_tool, "_cleanup_running", False)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)


def _fake_config(env_type="local"):
    return {
        "env_type": env_type,
        "docker_image": "fake-docker-image",
        "singularity_image": "docker://fake-singularity-image",
        "cwd": "/workspace",
        "host_cwd": None,
        "docker_mount_cwd_to_workspace": False,
        "timeout": 180,
        "lifetime_seconds": 300,
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "local_persistent": False,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "docker_volumes": [],
        "docker_forward_env": [],
        "docker_run_as_host_user": False,
    }


def test_get_or_create_active_env_creates_and_reuses(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    first = terminal_tool.get_or_create_active_env("task-1")
    second = terminal_tool.get_or_create_active_env("task-1")

    assert first is second
    assert len(created) == 1
    assert created[0]["env_type"] == "local"
    assert created[0]["cwd"] == "/workspace"
    assert created[0]["timeout"] == 180
    assert created[0]["task_id"] == "task-1"
    assert terminal_tool._active_environments["task-1"] is first
    assert "task-1" in terminal_tool._last_activity


def test_get_or_create_active_env_uses_timeout_for_new_environment(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    env = terminal_tool.get_or_create_active_env("task-timeout", timeout=42)

    assert isinstance(env, FakeEnv)
    assert created[0]["timeout"] == 42


def test_get_or_create_active_env_preserves_explicit_zero_timeout(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    env = terminal_tool.get_or_create_active_env("task-timeout-zero", timeout=0)

    assert isinstance(env, FakeEnv)
    assert created[0]["timeout"] == 0


def test_get_or_create_active_env_cleans_redundant_env_outside_env_lock(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class TrackingLock:
        def __init__(self):
            self.is_held = False

        def __enter__(self):
            assert not self.is_held
            self.is_held = True
            return self

        def __exit__(self, exc_type, exc, tb):
            self.is_held = False

    class LockCheckingEnv(FakeEnv):
        def __init__(self, lock):
            super().__init__()
            self.lock = lock
            self.cleanup_saw_lock_held = None

        def cleanup(self):
            self.cleanup_saw_lock_held = self.lock.is_held
            super().cleanup()

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    tracking_lock = TrackingLock()
    existing_env = FakeEnv()
    redundant_env = LockCheckingEnv(tracking_lock)

    def fake_create_environment(**kwargs):
        terminal_tool._active_environments["race-task"] = existing_env
        return redundant_env

    monkeypatch.setattr(terminal_tool, "_env_lock", tracking_lock)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    env = terminal_tool.get_or_create_active_env("race-task")

    assert env is existing_env
    assert redundant_env.cleaned is True
    assert redundant_env.cleanup_saw_lock_held is False


def test_get_or_create_active_env_builds_docker_config(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []
    config = _fake_config(env_type="docker")
    config["docker_mount_cwd_to_workspace"] = True
    config["host_cwd"] = "/home/miku/projects/langchain"
    config["docker_volumes"] = ["/tmp/cache:/cache"]
    config["docker_forward_env"] = ["CUSTOM_ENV"]
    config["docker_run_as_host_user"] = True

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    terminal_tool.get_or_create_active_env("docker-task")

    assert created[0]["env_type"] == "docker"
    assert created[0]["image"] == "fake-docker-image"
    assert created[0]["host_cwd"] == "/home/miku/projects/langchain"
    assert created[0]["container_config"]["docker_mount_cwd_to_workspace"] is True
    assert created[0]["container_config"]["docker_volumes"] == ["/tmp/cache:/cache"]
    assert created[0]["container_config"]["docker_forward_env"] == ["CUSTOM_ENV"]
    assert created[0]["container_config"]["docker_run_as_host_user"] is True
