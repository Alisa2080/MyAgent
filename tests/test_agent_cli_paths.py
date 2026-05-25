from pathlib import Path


def test_get_cli_home_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path / "custom-home"))

    from agent_cli.paths import get_cli_home

    assert get_cli_home() == (tmp_path / "custom-home").resolve()


def test_get_cli_home_defaults_to_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from agent_cli.paths import get_cli_home

    assert get_cli_home() == tmp_path / ".langchain-agent"


def test_get_db_path_points_under_cli_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.paths import get_db_path

    assert get_db_path() == tmp_path.resolve() / "cli.sqlite"
