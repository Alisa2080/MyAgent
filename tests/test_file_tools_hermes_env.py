import pytest

from agent_tools.file_toolkit import file_tools


class FakeEnv:
    cwd = "/workspace"

    def execute(self, command, **kwargs):
        return {"output": "ok\n", "returncode": 0}


@pytest.fixture(autouse=True)
def clear_file_ops_cache():
    file_tools.clear_file_ops_cache()
    yield
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
    assert calls == ["task-a"]


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

    assert calls == ["default"]
    assert file_ops.env is fake_env
    assert default_file_ops is file_ops


def test_resolve_path_uses_active_hermes_env_cwd_without_cached_wrapper(
    tmp_path, monkeypatch
):
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

    monkeypatch.setattr(file_tools, "get_active_env", fake_get_active_env, raising=False)
    monkeypatch.setattr(
        file_tools, "get_or_create_active_env", fake_get_or_create_active_env
    )

    resolved = file_tools._resolve_path_for_task("notes.txt", "task-a")

    assert resolved == tmp_path / "notes.txt"
    assert active_calls == ["task-a"]
    assert create_calls == []
