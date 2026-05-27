from agent_cli.doctor import (
    HealthCheck,
    check_dependencies,
    check_openai_api_key,
    check_python_version,
    check_workdir,
    doctor_exit_code,
    render_doctor_output,
    run_health_checks,
)


def test_check_python_version():
    result = check_python_version()
    assert "Python" in result.message
    assert result.status == "OK"


def test_check_dependencies():
    results = check_dependencies()
    # Each dependency is a separate HealthCheck
    assert len(results) >= 3
    # At least prompt_toolkit, langchain, langgraph should be present
    names = [r.name for r in results]
    for dep in ["prompt_toolkit", "langchain", "langgraph"]:
        assert dep in names


def test_check_workdir_exists():
    result = check_workdir("/home/miku/projects/langchain")
    assert result.status == "OK"
    assert "langchain" in result.message


def test_check_workdir_missing():
    result = check_workdir("/nonexistent/path")
    assert result.status == "FAIL"


def test_run_health_checks_returns_results():
    results = run_health_checks(workdir="/home/miku/projects/langchain")
    assert len(results) >= 3
    names = [r.name for r in results]
    assert "Python Version" in names
    # Dependencies are now individual checks
    assert "Platform" in names


def test_render_doctor_output_uses_stable_status_columns():
    results = [
        HealthCheck("Python Version", "OK", "Python 3.11"),
        HealthCheck("OPENAI_API_KEY", "WARN", "not set"),
        HealthCheck("SQLite DB", "FAIL", "cannot open"),
    ]

    output = render_doctor_output(results)

    assert "OK    Python Version" in output
    assert "WARN  OPENAI_API_KEY" in output
    assert "FAIL  SQLite DB" in output
    assert "✓" not in output
    assert "✗" not in output


def test_doctor_exit_code_is_zero_for_ok_and_warn():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("key", "WARN", "missing"),
    ]

    assert doctor_exit_code(checks) == 0


def test_doctor_exit_code_is_one_for_failures():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("db", "FAIL", "bad"),
    ]

    assert doctor_exit_code(checks) == 1


def test_check_openai_api_key_warns_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = check_openai_api_key()

    assert result.status == "WARN"
    assert result.name == "OPENAI_API_KEY"


def test_check_openai_api_key_ok_when_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = check_openai_api_key()

    assert result.status == "OK"
    assert "set" in result.message


def test_run_health_checks_reports_storage_and_openai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    by_name = {item.name: item for item in results}

    assert by_name["CLI Home"].status == "OK"
    assert by_name["SQLite DB"].status == "OK"
    assert by_name["Logs"].status == "OK"
    assert by_name["OPENAI_API_KEY"].status == "WARN"


def test_run_health_checks_uses_explicit_cli_home_for_database(tmp_path, monkeypatch):
    ambient_home = tmp_path / "ambient"
    explicit_home = tmp_path / "explicit"
    monkeypatch.setenv("AGENT_CLI_HOME", str(ambient_home))

    results = run_health_checks(workdir=str(tmp_path), cli_home=explicit_home)
    by_name = {item.name: item for item in results}

    assert by_name["SQLite DB"].status == "OK"
    assert by_name["SQLite DB"].message == str(explicit_home / "cli.sqlite")
    assert by_name["Background Tasks"].status in {"OK", "WARN"}
    assert (explicit_home / "cli.sqlite").exists()
    assert not (ambient_home / "cli.sqlite").exists()


def test_doctor_reports_stale_background_task_as_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_stale",
        session_id="session-stale",
        title="Stale",
        prompt_preview="work",
        owner_id=None,
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    background = next(item for item in results if item.name == "Background Tasks")

    assert background.status == "WARN"
    assert "stale" in background.message.lower() or "owner" in background.message.lower()


def test_doctor_reports_dead_background_owner_as_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.register_owner("dead-owner", process_id=99999999)
    store.create_task(
        task_id="bg_dead",
        session_id="session-dead",
        title="Dead",
        prompt_preview="work",
        owner_id="dead-owner",
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    background = next(item for item in results if item.name == "Background Tasks")

    assert background.status == "WARN"
    assert "dead" in background.message.lower() or "stale" in background.message.lower()


def test_doctor_reports_config_and_dotenv_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert str(tmp_path / "config.yaml") in config.message


def test_doctor_fails_semantically_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "display:\n  markdown: weird\n",
        encoding="utf-8",
    )

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert "display.markdown" in config.message


def test_doctor_fails_invalid_display_theme(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "display:\n  theme: weird\n",
        encoding="utf-8",
    )

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert "display.theme" in config.message
