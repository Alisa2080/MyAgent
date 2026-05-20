import json
from pathlib import Path

import pytest

from agent_tools.file_toolkit import file_tools


class FakeEnv:
    cwd = "/workspace"

    def execute(self, command, **kwargs):
        return {"output": "ok\n", "returncode": 0}


@pytest.fixture(autouse=True)
def clear_file_ops_cache():
    file_tools.clear_file_ops_cache()
    with file_tools._read_tracker_lock:
        file_tools._read_tracker.clear()
    yield
    with file_tools._read_tracker_lock:
        file_tools._read_tracker.clear()
    file_tools.clear_file_ops_cache()


def test_get_file_ops_wraps_hermes_active_env(monkeypatch):
    fake_env = FakeEnv()
    calls = []

    def fake_get_or_create_active_env(task_id):
        calls.append(task_id)
        return fake_env

    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    file_ops = file_tools._get_file_ops("task-a")

    assert calls == ["task-a"]
    assert isinstance(file_ops, file_tools.ShellFileOperations)
    assert file_ops.env is fake_env


def test_get_file_ops_reuses_cached_wrapper_for_same_env(monkeypatch):
    fake_env = FakeEnv()
    calls = []

    def fake_get_or_create_active_env(task_id):
        calls.append(task_id)
        return fake_env

    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    first = file_tools._get_file_ops("task-a")
    second = file_tools._get_file_ops("task-a")

    assert first is second
    assert calls == ["task-a", "task-a"]


def test_get_file_ops_replaces_cached_wrapper_when_active_env_changes(monkeypatch):
    first_env = FakeEnv()
    second_env = FakeEnv()
    envs = [first_env, second_env]

    def fake_get_or_create_active_env(task_id):
        assert task_id == "task-a"
        return envs.pop(0)

    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    first = file_tools._get_file_ops("task-a")
    second = file_tools._get_file_ops("task-a")

    assert first is not second
    assert first.env is first_env
    assert second.env is second_env
    assert file_tools._file_ops_cache["task-a"] is second


def test_get_file_ops_uses_default_for_falsy_task_id(monkeypatch):
    fake_env = FakeEnv()
    calls = []

    def fake_get_or_create_active_env(task_id):
        calls.append(task_id)
        return fake_env

    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    file_ops = file_tools._get_file_ops("")
    default_file_ops = file_tools._get_file_ops("default")

    assert calls == ["default", "default"]
    assert file_ops.env is fake_env
    assert default_file_ops is file_ops


def test_get_live_tracking_cwd_ignores_stale_cached_wrapper_without_active_env(
    tmp_path, monkeypatch
):
    cached_env = FakeEnv()
    cached_env.cwd = str(tmp_path)
    file_tools._file_ops_cache["task-a"] = file_tools.ShellFileOperations(cached_env)

    active_calls = []

    def fake_get_active_env(task_id):
        active_calls.append(task_id)
        return None

    monkeypatch.setattr(file_tools, "get_active_env", fake_get_active_env, raising=False)

    assert file_tools._get_live_tracking_cwd("task-a") is None
    assert active_calls == ["task-a"]


def test_resolve_path_uses_active_hermes_env_cwd_without_cached_wrapper(
    tmp_path, monkeypatch
):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    fake_env = FakeEnv()
    fake_env.cwd = str(tmp_path)
    active_calls = []
    create_calls = []

    def fake_get_active_env(task_id):
        active_calls.append(task_id)
        return fake_env

    def fake_get_or_create_active_env(task_id):
        create_calls.append(task_id)
        return fake_env

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(tmp_path)},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", fake_get_active_env)
    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    resolved = file_tools._resolve_path_for_task("notes.txt", "task-a")

    assert resolved == tmp_path / "notes.txt"
    assert active_calls == ["task-a"]
    assert create_calls == []


def test_resolve_path_for_task_uses_backend_policy_for_ssh_active_cwd(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class SSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "~"

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~"},
    )
    monkeypatch.setattr(
        terminal_tool,
        "get_active_env",
        lambda task_id: SSHEnv(),
    )

    resolved = file_tools._resolve_path_for_task("notes.txt", "task-ssh")

    assert resolved == Path("/home/remote/project/notes.txt")


def test_write_file_tool_denies_relative_path_in_active_local_env_cwd_outside_safe_root(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "safe-root"
    outside_root = tmp_path / "outside-root"
    safe_root.mkdir()
    outside_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))
    monkeypatch.chdir(safe_root)

    class RecordingEnv:
        cwd = str(outside_root)

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "4\n", "returncode": 0}

    env = RecordingEnv()
    file_ops = file_tools.ShellFileOperations(env)
    monkeypatch.setattr(file_tools, "get_active_env", lambda task_id: env)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: file_ops)

    raw = file_tools.write_file_tool("notes.txt", "leak", task_id="task-safe")
    payload = json.loads(raw)

    assert "Write denied" in payload["error"]
    assert env.commands == []


def test_shell_file_operations_allows_workspace_writes_for_docker_safe_root(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))
    monkeypatch.chdir(tmp_path)

    class DockerEnvironment:
        cwd = "/workspace"
        _hermes_env_type = "docker"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = DockerEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == ["/workspace", "/workspace"]


def test_shell_file_operations_allows_docker_workspace_absolute_write(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class DockerEnvironment:
        cwd = "/root"
        _hermes_env_type = "docker"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = DockerEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("/workspace/notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == ["/root", "/root", "/root"]


def test_shell_file_operations_allows_effective_docker_cwd_writes(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))
    monkeypatch.chdir(tmp_path)

    class DockerEnvironment:
        cwd = "/root"
        _hermes_env_type = "docker"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = DockerEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == ["/root", "/root"]


def test_shell_file_operations_allows_singularity_effective_cwd_writes(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SingularityEnvironment:
        cwd = "/project"
        _hermes_env_type = "singularity"
        _hermes_configured_cwd = "/configured"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = SingularityEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == ["/project", "/project"]


def test_shell_file_operations_keeps_workspace_root_denied_for_singularity(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SingularityEnvironment:
        cwd = "/project"
        _hermes_env_type = "singularity"
        _hermes_configured_cwd = "/configured"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = SingularityEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("/workspace/notes.txt", "hello")

    assert "Write denied" in result.error
    assert env.commands == []


def test_shell_file_operations_allows_effective_ssh_cwd_writes(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SSHEnvironment:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = SSHEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == [
        "/home/remote/project",
        "/home/remote/project",
    ]


def test_shell_file_operations_keeps_workspace_root_denied_for_ssh(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SSHEnvironment:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = SSHEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("/workspace/notes.txt", "hello")

    assert "Write denied" in result.error
    assert env.commands == []


def test_shell_file_operations_denies_host_safe_root_for_untagged_ssh_without_backend_roots(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SSHEnvironment:
        cwd = "~"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = SSHEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file(str(safe_root / "leak.txt"), "bad")

    assert "Write denied" in result.error
    assert env.commands == []


def test_shell_file_operations_denies_config_host_safe_root_for_untagged_ssh(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-safe-root"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SSHEnvironment:
        class Config:
            cwd = str(safe_root)

        config = Config()

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = SSHEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file(str(safe_root / "leak.txt"), "bad")

    assert "Write denied" in result.error
    assert env.commands == []


@pytest.mark.parametrize("env_type", ["docker", "ssh"])
def test_shell_file_operations_denies_host_safe_root_absolute_path_outside_backend_roots(
    tmp_path, monkeypatch, env_type
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))
    target = safe_root / "outside-backend.txt"

    class BackendEnvironment:
        _hermes_env_type = env_type
        cwd = "/workspace" if env_type == "docker" else "/home/remote/project"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = BackendEnvironment()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file(str(target), "hello")

    assert "Write denied" in result.error
    assert env.commands == []


def test_invalidate_dedup_for_path_resolves_with_supplied_task_id(monkeypatch):
    resolved_for_task = "/task-a/notes.txt"
    file_tools._read_tracker.clear()
    file_tools._read_tracker["task-a"] = {
        "dedup": {
            (resolved_for_task, 1, 500): 123.0,
            ("/task-a/other.txt", 1, 500): 456.0,
        }
    }

    def fake_resolve_path_for_task(filepath, task_id):
        return Path(f"/{task_id}/{filepath}")

    monkeypatch.setattr(
        file_tools, "_resolve_path_for_task", fake_resolve_path_for_task
    )

    file_tools._invalidate_dedup_for_path("notes.txt", "task-a")

    assert (resolved_for_task, 1, 500) not in file_tools._read_tracker["task-a"]["dedup"]
    assert ("/task-a/other.txt", 1, 500) in file_tools._read_tracker["task-a"]["dedup"]


def test_patch_tool_move_file_tracks_source_and_destination(monkeypatch):
    class PatchResult:
        def to_dict(self):
            return {"status": "success"}

    class FakeFileOps:
        def __init__(self):
            self.patches = []

        def patch_v4a(self, patch):
            self.patches.append(patch)
            return PatchResult()

    class RecordingLock:
        def __init__(self, path, entered):
            self.path = path
            self.entered = entered

        def __enter__(self):
            self.entered.append(self.path)

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_ops = FakeFileOps()
    checked_sensitive = []
    resolved_paths = []
    stale_checked = []
    timestamp_updates = []
    note_writes = []
    lock_entries = []
    patch_content = "\n".join(
        [
            "*** Begin Patch",
            "*** Move File: source.txt -> dest.txt",
            "*** End Patch",
        ]
    )

    def fake_resolve(path, task_id):
        resolved_paths.append((path, task_id))
        return f"/workspace/{path}"

    def fake_sensitive(path, task_id):
        checked_sensitive.append((path, task_id))
        return None

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(file_tools, "_resolve_path_for_task", fake_resolve)
    monkeypatch.setattr(file_tools, "_check_sensitive_path", fake_sensitive)
    monkeypatch.setattr(
        file_tools.file_state,
        "lock_path",
        lambda path: RecordingLock(path, lock_entries),
    )
    monkeypatch.setattr(
        file_tools.file_state,
        "check_stale",
        lambda task_id, path, *, current_mtime=None: stale_checked.append(
            (task_id, path, current_mtime)
        )
        or None,
    )
    monkeypatch.setattr(
        file_tools,
        "_update_read_timestamp",
        lambda path, task_id: timestamp_updates.append((path, task_id)) or None,
    )
    monkeypatch.setattr(
        file_tools.file_state,
        "note_write",
        lambda task_id, path, *, mtime=None: note_writes.append(
            (task_id, path, mtime)
        ),
    )

    raw = file_tools.patch_tool(
        mode="patch",
        patch=patch_content,
        task_id="task-move",
    )

    assert raw == '{"status": "success"}'
    assert checked_sensitive == [
        ("source.txt", "task-move"),
        ("dest.txt", "task-move"),
    ]
    assert set(lock_entries) == {"/workspace/source.txt", "/workspace/dest.txt"}
    assert stale_checked == [
        ("task-move", "/workspace/source.txt", None),
        ("task-move", "/workspace/dest.txt", None),
    ]
    assert timestamp_updates == [
        ("source.txt", "task-move"),
        ("dest.txt", "task-move"),
    ]
    assert note_writes == [
        ("task-move", "/workspace/source.txt", None),
        ("task-move", "/workspace/dest.txt", None),
    ]
    assert fake_ops.patches == [patch_content]


def test_sample_mtime_degrades_when_backend_context_lookup_fails_without_host_stat(
    monkeypatch,
):
    monkeypatch.setattr(
        file_tools,
        "get_backend_path_context",
        lambda task_id: (_ for _ in ()).throw(RuntimeError("context unavailable")),
    )
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    assert file_tools._sample_mtime_for_task(
        "/workspace/notes.txt",
        "docker-task",
    ) is None


def test_read_file_tool_uses_backend_mtime_for_non_local_dedup(monkeypatch):
    from agent_tools.file_toolkit.result_models import ReadResult
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class DockerEnv:
        cwd = "/workspace"
        _hermes_env_type = "docker"
        _hermes_configured_cwd = "/workspace"

    class FakeFileOps:
        def __init__(self):
            self.reads = 0
            self.stats = []

        def stat_mtime(self, path):
            self.stats.append(path)
            return 1716200000.0

        def read_file(self, path, offset=1, limit=500):
            self.reads += 1
            return ReadResult(
                content="     1|hello",
                total_lines=1,
            )

    fake_ops = FakeFileOps()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/workspace", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: DockerEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    first = json.loads(file_tools.read_file_tool("notes.txt", task_id="docker-task"))
    second = json.loads(file_tools.read_file_tool("notes.txt", task_id="docker-task"))

    assert "error" not in first
    assert second["message"].startswith("File unchanged since last read")
    assert fake_ops.reads == 1
    assert fake_ops.stats == ["/workspace/notes.txt", "/workspace/notes.txt"]


def test_read_file_tool_records_unknown_backend_mtime_explicitly(monkeypatch):
    from agent_tools.file_toolkit.result_models import ReadResult
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class SSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "/home/remote/project"

    class FakeFileOps:
        def stat_mtime(self, path):
            return None

        def read_file(self, path, offset=1, limit=500):
            return ReadResult(content="     1|hello", total_lines=1)

    recorded = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "/home/remote/project", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: SSHEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: FakeFileOps())
    monkeypatch.setattr(
        file_tools.file_state,
        "record_read",
        lambda task_id, path, *, partial=False, mtime=None: recorded.append(
            (task_id, path, partial, mtime)
        ),
    )
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    payload = json.loads(file_tools.read_file_tool("notes.txt", task_id="ssh-task"))

    assert "error" not in payload
    assert recorded == [("ssh-task", "/home/remote/project/notes.txt", False, None)]


def test_read_file_tool_repeats_full_read_when_backend_mtime_unknown(monkeypatch):
    from agent_tools.file_toolkit.result_models import ReadResult
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class SSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "/home/remote/project"

    class FakeFileOps:
        def __init__(self):
            self.reads = 0

        def stat_mtime(self, path):
            return None

        def read_file(self, path, offset=1, limit=500):
            self.reads += 1
            return ReadResult(
                content=f"     1|hello {self.reads}",
                total_lines=1,
            )

    fake_ops = FakeFileOps()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "/home/remote/project", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: SSHEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    first = json.loads(file_tools.read_file_tool("notes.txt", task_id="ssh-task"))
    second = json.loads(file_tools.read_file_tool("notes.txt", task_id="ssh-task"))

    assert first["content"] == "     1|hello 1"
    assert second["content"] == "     1|hello 2"
    assert "dedup" not in second
    assert fake_ops.reads == 2


def test_write_file_tool_passes_backend_mtime_to_stale_and_note_write(monkeypatch):
    from agent_tools.file_toolkit.result_models import WriteResult
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class DockerEnv:
        cwd = "/workspace"
        _hermes_env_type = "docker"
        _hermes_configured_cwd = "/workspace"

    class FakeFileOps:
        def __init__(self):
            self.stats = []

        def stat_mtime(self, path):
            self.stats.append(path)
            return 1716200300.0

        def write_file(self, path, content):
            return WriteResult(bytes_written=len(content))

    class NoopLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_ops = FakeFileOps()
    stale_checks = []
    note_writes = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/workspace", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: DockerEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(file_tools.file_state, "lock_path", lambda path: NoopLock())
    monkeypatch.setattr(
        file_tools.file_state,
        "check_stale",
        lambda task_id, path, *, current_mtime=None: stale_checks.append(
            (task_id, path, current_mtime)
        )
        or None,
    )
    monkeypatch.setattr(
        file_tools.file_state,
        "note_write",
        lambda task_id, path, *, mtime=None: note_writes.append((task_id, path, mtime)),
    )
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    payload = json.loads(
        file_tools.write_file_tool("notes.txt", "hello", task_id="docker-task")
    )

    assert "error" not in payload
    assert stale_checks == [("docker-task", "/workspace/notes.txt", 1716200300.0)]
    assert note_writes == [("docker-task", "/workspace/notes.txt", 1716200300.0)]
    assert fake_ops.stats == ["/workspace/notes.txt", "/workspace/notes.txt"]


def test_write_file_tool_smokes_real_local_hermes_env(tmp_path, monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool
    from contextlib import suppress

    task_id = "task-real-file"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(tmp_path))

    file_tools.clear_file_ops_cache(task_id)
    terminal_tool.cleanup_vm(task_id)

    try:
        raw = file_tools.write_file_tool("notes.txt", "hello world", task_id=task_id)
        payload = json.loads(raw)
        target = tmp_path / "notes.txt"

        assert "error" not in payload
        assert payload["bytes_written"] == len("hello world")
        assert target.exists()
        assert target.read_text() == "hello world"

        active_env = terminal_tool.get_active_env(task_id)
        assert active_env is not None
        assert file_tools._get_file_ops(task_id).env is active_env

        read_payload = json.loads(file_tools.read_file_tool("notes.txt", task_id=task_id))
        assert "error" not in read_payload
        assert read_payload["content"].split("|", 1)[1] == "hello world"
    finally:
        with suppress(Exception):
            terminal_tool.cleanup_vm(task_id)
        file_tools.clear_file_ops_cache(task_id)
