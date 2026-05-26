from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace


def test_main_sessions_prints_sessions_without_checkpointer(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.session_store import SessionStore

    store = SessionStore(tmp_path / "cli.sqlite")
    store.create_session(workdir="/repo", model="m", title="Existing")

    import agent_cli.main as main_module

    def fail_create_checkpointer(path):
        raise AssertionError("sessions must not create a checkpointer")

    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", fail_create_checkpointer
    )

    code = main_module.main(["sessions"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Existing" in out


def test_main_ask_invokes_cli_with_checkpointer_from_handle(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    created = []

    class FakeCLI:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def submit_message(self, text):
            return f"answer: {text}"

    handle = SimpleNamespace(checkpointer="cp", closed=False)

    def close_handle():
        handle.closed = True

    handle.close = close_handle

    import agent_cli.main as main_module

    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", lambda path: handle
    )

    code = main_module.main(["ask", "hello"])

    assert code == 0
    assert "answer: hello" in capsys.readouterr().out
    assert created[0]["checkpointer"] == "cp"
    assert handle.closed is True


def test_main_chat_resume_rejects_unknown_session_without_checkpointer(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    def fail_create_checkpointer(path):
        raise AssertionError("unknown resume must not create a checkpointer")

    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", fail_create_checkpointer
    )

    code = main_module.main(["chat", "--resume", "missing"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Unknown session: missing" in captured.err


def test_main_ask_closes_checkpointer_handle_when_submit_raises(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    class FakeCLI:
        def __init__(self, **kwargs):
            pass

        def submit_message(self, text):
            raise RuntimeError("boom")

    handle = SimpleNamespace(checkpointer="cp", closed=False)

    def close_handle():
        handle.closed = True

    handle.close = close_handle

    import agent_cli.main as main_module

    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", lambda path: handle
    )

    code = main_module.main(["ask", "hello"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Error: boom" in captured.err
    assert handle.closed is True


def test_main_ask_checkpoint_dependency_error_names_command_and_package(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.checkpoints import CheckpointDependencyError

    def fail_create_checkpointer(path):
        raise CheckpointDependencyError(
            "Install langgraph-checkpoint-sqlite to use SQLite checkpointing."
        )

    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", fail_create_checkpointer
    )

    code = main_module.main(["ask", "hello"])

    captured = capsys.readouterr()
    assert code == 2
    assert "ask" in captured.err
    assert "langgraph-checkpoint-sqlite" in captured.err


def test_main_chat_closes_checkpointer_handle_when_repl_raises(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    class FakeCLI:
        def __init__(self, **kwargs):
            pass

        def run_repl(self):
            raise RuntimeError("boom")

    handle = SimpleNamespace(checkpointer="cp", closed=False)

    def close_handle():
        handle.closed = True

    handle.close = close_handle

    import agent_cli.main as main_module

    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", lambda path: handle
    )

    try:
        main_module.main(["chat"])
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected run_repl to raise")

    assert handle.closed is True


def test_python_module_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_python_module_sessions_smoke(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "sessions"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "AGENT_CLI_HOME": str(tmp_path)},
    )

    assert result.returncode == 0
    assert "No sessions found." in result.stdout
