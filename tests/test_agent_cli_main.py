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


def test_python_module_profile_sessions_smoke(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "--profile", "dev", "sessions"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(tmp_path), "AGENT_CLI_HOME": ""},
    )

    assert result.returncode == 0
    assert "No sessions found." in result.stdout
    assert (tmp_path / ".langchain-agent" / "profiles" / "dev" / "cli.sqlite").exists()


def test_main_argv_none_preserves_public_options_before_subcommand(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path / "home"))
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent_cli", "--workdir", str(missing), "doctor"],
    )

    import agent_cli.main as main_module

    code = main_module.main()

    captured = capsys.readouterr()
    assert code == 1
    assert "FAIL" in captured.out
    assert "Workdir" in captured.out
    assert str(missing) in captured.out


def test_main_doctor_reports_invalid_cli_home_without_traceback(
    monkeypatch, tmp_path, capsys
):
    cli_home_file = tmp_path / "not-a-dir"
    cli_home_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AGENT_CLI_HOME", str(cli_home_file))

    import agent_cli.main as main_module

    code = main_module.main(["doctor", "--workdir", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert "FAIL" in captured.out
    assert "CLI Home" in captured.out
    assert str(cli_home_file) in captured.out
    assert "Traceback" not in captured.err


def test_main_invalid_profile_returns_code_2(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)

    import agent_cli.main as main_module

    code = main_module.main(["--profile", "../bad", "sessions"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Invalid profile" in captured.err


def test_main_model_config_used_when_cli_model_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  name: config-model\n", encoding="utf-8")
    created = []

    class FakeCLI:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def run_repl(self):
            return 0

    import agent_cli.main as main_module

    handle = type("Handle", (), {"checkpointer": "cp", "close": lambda self: None})()
    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(main_module, "create_sqlite_checkpointer", lambda path: handle)

    assert main_module.main(["chat"]) == 0
    assert created[0]["model_name"] == "config-model"


def test_main_cli_model_overrides_config_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  name: config-model\n", encoding="utf-8")
    created = []

    class FakeCLI:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def run_repl(self):
            return 0

    import agent_cli.main as main_module

    handle = type("Handle", (), {"checkpointer": "cp", "close": lambda self: None})()
    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(main_module, "create_sqlite_checkpointer", lambda path: handle)

    assert main_module.main(["--model", "cli-model", "chat"]) == 0
    assert created[0]["model_name"] == "cli-model"


def test_main_malformed_config_returns_code_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")

    import agent_cli.main as main_module

    code = main_module.main(["sessions"])

    captured = capsys.readouterr()
    assert code == 2
    assert "config.yaml" in captured.err


def test_main_invalid_display_markdown_returns_code_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("display:\n  markdown: weird\n", encoding="utf-8")

    import agent_cli.main as main_module

    code = main_module.main(["sessions"])

    captured = capsys.readouterr()
    assert code == 2
    assert "display.markdown" in captured.err


def test_settings_from_config_reads_display_theme(tmp_path):
    from agent_cli.config import settings_from_config

    (tmp_path / "config.yaml").write_text("display:\n  theme: slate\n", encoding="utf-8")

    settings = settings_from_config(cli_home=tmp_path, profile=None, cli_model=None)

    assert settings.display_theme == "slate"


def test_settings_from_config_rejects_invalid_display_theme(tmp_path):
    from agent_cli.config import ConfigError, settings_from_config

    (tmp_path / "config.yaml").write_text("display:\n  theme: weird\n", encoding="utf-8")

    try:
        settings_from_config(cli_home=tmp_path, profile=None, cli_model=None)
    except ConfigError as exc:
        assert "display.theme" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_settings_from_config_rejects_falsy_display_theme(tmp_path):
    from agent_cli.config import ConfigError, settings_from_config

    (tmp_path / "config.yaml").write_text("display:\n  theme: false\n", encoding="utf-8")

    try:
        settings_from_config(cli_home=tmp_path, profile=None, cli_model=None)
    except ConfigError as exc:
        assert "display.theme" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_make_cli_wires_background_registry(tmp_path):
    from types import SimpleNamespace

    from agent_cli.config import RuntimeSettings
    from agent_cli.main import make_cli
    from agent_cli.session_store import SessionStore

    args = SimpleNamespace(workdir=str(tmp_path), resume=None)
    store = SessionStore(tmp_path / "cli.sqlite")
    cli = make_cli(
        args=args,
        store=store,
        checkpointer=object(),
        settings=RuntimeSettings(
            profile="dev",
            cli_home=tmp_path,
            model_name="model",
            display_theme="slate",
        ),
    )

    assert cli.background_registry is not None
    assert cli.profile == "dev"
    assert cli.cli_home == str(tmp_path)
    assert cli.display_theme == "slate"


def test_make_cli_background_runner_uses_fresh_checkpointer_handle(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from agent_cli.config import RuntimeSettings
    from agent_cli.main import make_cli
    from agent_cli.session_store import SessionStore

    import agent_cli.main as main_module

    handles = []
    runner_calls = []

    class Handle:
        def __init__(self):
            self.checkpointer = object()
            self.closed = False

        def close(self):
            self.closed = True

    def fake_create_sqlite_checkpointer(path):
        handle = Handle()
        handles.append((path, handle))
        return handle

    monkeypatch.setattr(
        main_module, "create_sqlite_checkpointer", fake_create_sqlite_checkpointer
    )
    monkeypatch.setattr(
        main_module,
        "default_agent_factory",
        lambda checkpointer: {"checkpointer": checkpointer},
    )

    def fake_runner(agent, input_data, config):
        runner_calls.append((agent, input_data, config))
        return {"messages": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(main_module, "default_runner", fake_runner)

    args = SimpleNamespace(workdir=str(tmp_path), resume=None)
    store = SessionStore(tmp_path / "cli.sqlite")
    cli = make_cli(
        args=args,
        store=store,
        checkpointer=object(),
        settings=RuntimeSettings(
            profile="dev",
            cli_home=tmp_path,
            model_name="model",
            display_theme="slate",
        ),
    )

    record = cli.background_registry.start("background work")
    cli.background_registry.join(record.task_id, timeout=2)

    assert len(handles) == 1
    assert handles[0][0] == store.db_path
    assert handles[0][1].closed is True
    assert runner_calls[0][0]["checkpointer"] is handles[0][1].checkpointer


def test_main_doctor_reports_malformed_config_without_startup_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")

    import agent_cli.main as main_module

    code = main_module.main(["doctor"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Config" in captured.out
    assert "config.yaml" in captured.out


def test_main_doctor_returns_1_when_health_check_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.doctor as doctor_module

    # Monkeypatch run_health_checks at the source module level
    original_run = doctor_module.run_health_checks
    doctor_module.run_health_checks = lambda workdir, cli_home=None: [
        doctor_module.HealthCheck("config", "FAIL", "bad config")
    ]

    try:
        import agent_cli.main as main_module

        code = main_module.main(["doctor"])

        assert code == 1
    finally:
        doctor_module.run_health_checks = original_run


def test_main_doctor_returns_0_for_warn_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.doctor as doctor_module

    original_run = doctor_module.run_health_checks
    doctor_module.run_health_checks = lambda workdir, cli_home=None: [
        doctor_module.HealthCheck("OPENAI_API_KEY", "WARN", "not set")
    ]

    try:
        import agent_cli.main as main_module

        code = main_module.main(["doctor"])

        assert code == 0
    finally:
        doctor_module.run_health_checks = original_run


def test_parser_accepts_public_options_after_doctor_subcommand():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(
        ["doctor", "--workdir", "/tmp/repo", "--profile", "dev", "--model", "m"]
    )

    assert args.command == "doctor"
    assert args.workdir == "/tmp/repo"
    assert args.profile == "dev"
    assert args.model == "m"


def test_parser_accepts_public_options_after_sessions_subcommand():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(["sessions", "--profile", "dev"])

    assert args.command == "sessions"
    assert args.profile == "dev"


def test_parser_accepts_model_after_ask_subcommand_and_preserves_question():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(["ask", "--model", "m", "hello", "world"])

    assert args.command == "ask"
    assert args.model == "m"
    assert args.question == ["hello", "world"]


def test_main_invalid_profile_after_subcommand_returns_code_2(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)

    import agent_cli.main as main_module

    code = main_module.main(["sessions", "--profile", "../bad"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Invalid profile" in captured.err


def test_main_config_set_and_get_round_trip(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    set_code = main_module.main(["config", "set", "display.markdown", "strip"])
    get_code = main_module.main(["config", "get", "display.markdown"])

    captured = capsys.readouterr()
    assert set_code == 0
    assert get_code == 0
    assert "strip" in captured.out


def test_main_config_set_rejects_unknown_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    code = main_module.main(["config", "set", "unknown.path", "value"])

    captured = capsys.readouterr()
    assert code == 2
    assert "unknown.path" in captured.err
