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
        "container_network": True,
        "docker_volumes": [],
        "docker_forward_env": [],
        "docker_run_as_host_user": False,
    }


def test_get_env_config_uses_profile_default_for_hosted(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_CONTAINER_NETWORK", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["container_network"] is False


def test_get_env_config_hosted_ignores_container_network_override(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    monkeypatch.setenv("TERMINAL_CONTAINER_NETWORK", "true")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["container_network"] is False


def test_get_env_config_respects_explicit_terminal_env(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    assert terminal_tool._get_env_config()["env_type"] == "local"


def test_get_env_config_dev_keeps_network_default(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_CONTAINER_NETWORK", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert config["container_network"] is True


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
    assert created[0]["container_config"]["container_network"] is True
    assert created[0]["container_config"]["docker_volumes"] == ["/tmp/cache:/cache"]
    assert created[0]["container_config"]["docker_forward_env"] == ["CUSTOM_ENV"]
    assert created[0]["container_config"]["docker_run_as_host_user"] is True


def test_cleanup_vm_clears_file_ops_cache_when_env_exists(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    env = FakeEnv()
    cleared = []
    terminal_tool._active_environments["task-clean"] = env
    terminal_tool._last_activity["task-clean"] = 123.0
    terminal_tool._creation_locks["task-clean"] = object()

    monkeypatch.setattr(
        terminal_tool,
        "_clear_file_ops_cache_for_task",
        lambda task_id: cleared.append(task_id),
        raising=False,
    )

    terminal_tool.cleanup_vm("task-clean")

    assert cleared == ["task-clean"]
    assert env.cleaned is True
    assert "task-clean" not in terminal_tool._active_environments
    assert "task-clean" not in terminal_tool._last_activity
    assert "task-clean" not in terminal_tool._creation_locks


def test_cleanup_vm_clears_file_ops_cache_without_env(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    cleared = []

    monkeypatch.setattr(
        terminal_tool,
        "_clear_file_ops_cache_for_task",
        lambda task_id: cleared.append(task_id),
        raising=False,
    )

    terminal_tool.cleanup_vm("missing-task")

    assert cleared == ["missing-task"]


def test_cleanup_inactive_envs_clears_file_ops_cache_for_reaped_env(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    stale_env = FakeEnv()
    fresh_env = FakeEnv()
    terminal_tool._active_environments["stale-task"] = stale_env
    terminal_tool._active_environments["fresh-task"] = fresh_env
    terminal_tool._last_activity["stale-task"] = 100.0
    terminal_tool._last_activity["fresh-task"] = 395.0
    terminal_tool._creation_locks["stale-task"] = object()
    terminal_tool._creation_locks["fresh-task"] = object()
    cleared = []

    monkeypatch.setattr(terminal_tool.time, "time", lambda: 500.0)
    monkeypatch.setattr(
        terminal_tool.process_registry,
        "has_active_processes",
        lambda task_id: False,
    )
    monkeypatch.setattr(
        terminal_tool,
        "_clear_file_ops_cache_for_task",
        lambda task_id: cleared.append(task_id),
        raising=False,
    )

    terminal_tool._cleanup_inactive_envs(lifetime_seconds=300)

    assert cleared == ["stale-task"]
    assert stale_env.cleaned is True
    assert fresh_env.cleaned is False
    assert "stale-task" not in terminal_tool._active_environments
    assert "fresh-task" in terminal_tool._active_environments
    assert "stale-task" not in terminal_tool._creation_locks
    assert "fresh-task" in terminal_tool._creation_locks


def test_terminal_tool_uses_get_or_create_active_env(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    fake_env = FakeEnv(cwd="/workspace")
    helper_calls = []

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        helper_calls.append({"task_id": task_id, "workdir": workdir, "timeout": timeout})
        return fake_env

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", fake_get_or_create_active_env)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    raw = terminal_tool.terminal_tool(
        command="printf ok",
        background=False,
        timeout=7,
        task_id="task-terminal",
        force=False,
        workdir="/workspace/subdir",
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert helper_calls == [
        {"task_id": "task-terminal", "workdir": "/workspace/subdir", "timeout": 7}
    ]
    assert fake_env.execute_calls == [
        ("printf ok", {"timeout": 7, "cwd": "/workspace/subdir"})
    ]


def test_terminal_tool_acquires_env_before_approval_guard(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    call_order = []

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        call_order.append("helper")
        return FakeEnv(cwd="/workspace")

    def fake_check_all_guards(command, env_type):
        call_order.append("guard")
        return {
            "approved": False,
            "description": "blocked for test",
            "message": "denied for test",
        }

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", fake_get_or_create_active_env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", fake_check_all_guards)

    raw = terminal_tool.terminal_tool(
        command="printf blocked",
        background=False,
        timeout=7,
        task_id="task-terminal-guard",
        force=False,
    )
    payload = json.loads(raw)

    assert payload["status"] == "blocked"
    assert payload["error"] == "denied for test"
    assert call_order == ["helper", "guard"]


def test_create_environment_tags_local_env_metadata(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class FakeLocalEnv(FakeEnv):
        def __init__(self, cwd, timeout):
            super().__init__(cwd=cwd)
            self.timeout = timeout

    monkeypatch.setattr(
        terminal_tool,
        "LocalEnvironment",
        lambda cwd, timeout: FakeLocalEnv(cwd=cwd, timeout=timeout),
    )

    env = terminal_tool._create_environment(
        env_type="local",
        image="",
        cwd="/repo",
        timeout=33,
    )

    assert env._hermes_env_type == "local"
    assert env._hermes_configured_cwd == "/repo"
    assert env._hermes_host_cwd is None


def test_create_environment_tags_docker_env_metadata(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class FakeDockerEnv(FakeEnv):
        def __init__(self, **kwargs):
            super().__init__(cwd=kwargs["cwd"])
            self.kwargs = kwargs

    monkeypatch.setattr(
        terminal_tool,
        "DockerEnvironment",
        lambda **kwargs: FakeDockerEnv(**kwargs),
    )

    env = terminal_tool._create_environment(
        env_type="docker",
        image="fake-image",
        cwd="/workspace",
        timeout=44,
        container_config={"container_persistent": True},
        task_id="task-docker",
        host_cwd="/home/miku/projects/langchain",
    )

    assert env._hermes_env_type == "docker"
    assert env._hermes_configured_cwd == "/workspace"
    assert env._hermes_host_cwd == "/home/miku/projects/langchain"
