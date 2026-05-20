from pathlib import Path

import pytest


class FakeEnv:
    def __init__(self, cwd, env_type, configured_cwd=None):
        self.cwd = cwd
        self._hermes_env_type = env_type
        self._hermes_configured_cwd = (
            configured_cwd if configured_cwd is not None else cwd
        )
        self._hermes_host_cwd = None


def test_policy_uses_active_ssh_cwd_without_host_expanding_tilde(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/home/remote/project", "ssh")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )

    ctx = backend_paths.get_backend_path_context("task-ssh")

    assert ctx.env_type == "ssh"
    assert ctx.cwd == "/home/remote/project"
    assert (
        backend_paths.resolve_path_for_policy("notes.txt", "task-ssh")
        == "/home/remote/project/notes.txt"
    )
    assert (
        "/home/remote/project"
        in backend_paths.allowed_workspace_roots_for_task("task-ssh")
    )
    assert (
        str(Path.home())
        not in backend_paths.allowed_workspace_roots_for_task("task-ssh")
    )


def test_policy_allows_docker_workspace_even_when_cwd_is_root(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/root", "docker")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-docker")

    assert "/workspace" in roots
    assert "/root" in roots
    assert (
        backend_paths.resolve_path_for_policy("/workspace/app.py", "task-docker")
        == "/workspace/app.py"
    )


def test_policy_rejects_host_expanded_home_as_ssh_configured_root(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/home/remote/project", "ssh", configured_cwd="~")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": str(Path.home()), "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-ssh-expanded")

    assert "/home/remote/project" in roots
    assert str(Path.home()) not in roots


def test_policy_excludes_stale_docker_configured_cwd_matching_host_home(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/root", "docker", configured_cwd=None)
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": str(Path.home()), "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-docker-home")

    assert "/workspace" in roots
    assert "/root" in roots
    assert str(Path.home()) not in roots


def test_safe_write_roots_excludes_distinct_host_cwd_for_non_local_backend():
    from agent_core.workspace import WORKDIR
    from agent_tools.file_toolkit import backend_paths

    env = FakeEnv("/root/project", "docker", configured_cwd="/app")
    env._hermes_host_cwd = "/host/project"

    roots = backend_paths.safe_write_roots_for_env(env)

    assert str(WORKDIR.resolve()) not in roots
    assert "/workspace" in roots
    assert "/root/project" in roots
    assert "/app" in roots
    assert "/host/project" not in roots


def test_allowed_workspace_roots_keep_host_workspace_for_docker_task(monkeypatch):
    from agent_core.workspace import WORKDIR
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/root/project", "docker", configured_cwd="/app")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/stale", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-docker-public")

    assert str(WORKDIR.resolve()) in roots
    assert "/workspace" in roots


@pytest.mark.parametrize("path", ["~", "~/outside.txt", "~user/file"])
def test_policy_rejects_non_local_tilde_paths_without_backend_home(monkeypatch, path):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/home/remote/project", "ssh")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "/stale", "host_cwd": None},
    )

    with pytest.raises(ValueError):
        backend_paths.resolve_path_for_policy(path, "task-ssh-tilde")


def test_policy_prefers_active_configured_cwd_over_stale_global_config(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/root/project", "docker", configured_cwd="/active/configured")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/stale/configured", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-docker-configured")

    assert "/active/configured" in roots
    assert "/stale/configured" not in roots


def test_policy_uses_singularity_active_cwd_without_stale_config_root(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/analysis/project", "singularity")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "singularity", "cwd": "/root", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-singularity")

    assert "/analysis/project" in roots
    assert "/root" not in roots
    assert "/workspace" not in roots
    assert (
        backend_paths.resolve_path_for_policy("notes.txt", "task-singularity")
        == "/analysis/project/notes.txt"
    )
