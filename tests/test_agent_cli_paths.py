from pathlib import Path

import pytest

from agent_cli.config import apply_profile_override, validate_profile_name


def test_validate_profile_name_accepts_safe_names():
    assert validate_profile_name("dev") == "dev"
    assert validate_profile_name("work.profile-1") == "work.profile-1"


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".."])
def test_validate_profile_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_profile_name(name)


def test_apply_profile_override_sets_home_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    applied = apply_profile_override(["--profile", "dev", "sessions"])

    assert applied.profile == "dev"
    assert applied.used_existing_home is False
    assert Path(applied.cli_home) == tmp_path / ".langchain-agent" / "profiles" / "dev"
    assert Path(applied.cli_home) == Path(applied.env_value)


def test_apply_profile_override_respects_existing_agent_cli_home(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("AGENT_CLI_HOME", str(explicit))

    applied = apply_profile_override(["--profile", "dev", "sessions"])

    assert applied.profile == "dev"
    assert applied.used_existing_home is True
    assert Path(applied.cli_home) == explicit.resolve()


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
