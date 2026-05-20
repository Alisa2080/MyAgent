from pathlib import Path


class FakeEnv:
    def __init__(self, cwd, env_type):
        self.cwd = cwd
        self._hermes_env_type = env_type
        self._hermes_configured_cwd = cwd
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


def test_policy_uses_singularity_active_cwd_without_global_workspace_root(monkeypatch):
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
    assert "/root" in roots
    assert "/workspace" not in roots
    assert (
        backend_paths.resolve_path_for_policy("notes.txt", "task-singularity")
        == "/analysis/project/notes.txt"
    )
