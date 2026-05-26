from agent_cli.doctor import (
    check_python_version,
    check_dependencies,
    check_workdir,
    run_health_checks,
    render_doctor_output,
    HealthCheck,
)


def test_check_python_version():
    ok, message = check_python_version()
    assert "Python" in message
    assert ok is True


def test_check_dependencies():
    ok, message = check_dependencies()
    if not ok:
        assert "Missing" in message
    else:
        assert "available" in message


def test_check_workdir_exists():
    ok, message = check_workdir("/home/miku/projects/langchain")
    assert ok is True
    assert "langchain" in message


def test_check_workdir_missing():
    ok, message = check_workdir("/nonexistent/path")
    assert ok is False


def test_run_health_checks_returns_results():
    results = run_health_checks(workdir="/home/miku/projects/langchain")
    assert len(results) >= 3
    names = [r.name for r in results]
    assert "Python Version" in names
    assert "Dependencies" in names


def test_render_doctor_output_shows_status():
    results = [
        HealthCheck("Test Check", "OK", "OK"),
        HealthCheck("Failing Check", "FAIL", "Error"),
    ]
    output = render_doctor_output(results)
    assert "✓" in output
    assert "✗" in output
    assert "Test Check" in output
    assert "Failing Check" in output


def test_doctor_reports_config_and_dotenv_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.doctor import render_doctor_output, run_health_checks

    output = render_doctor_output(run_health_checks(workdir=str(tmp_path)))

    assert "config" in output.lower()
    assert "dotenv" in output.lower()


def test_doctor_uses_cli_home_and_validates_config(tmp_path, monkeypatch):
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
