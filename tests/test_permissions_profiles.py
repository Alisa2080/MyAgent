import pytest


def test_explicit_profile_wins(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "hosted"


def test_invalid_explicit_profile_raises(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "staging")

    with pytest.raises(ValueError, match="Invalid AGENT_RUNTIME_PROFILE"):
        resolve_runtime_profile()


def test_profile_inference_order(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AGENT_HOSTED", "true")
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "prod"


def test_hosted_inference(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("AGENT_HOSTED", "true")

    assert resolve_runtime_profile() == "hosted"


def test_ci_inference(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.delenv("AGENT_HOSTED", raising=False)
    monkeypatch.delenv("LANGGRAPH_DEPLOYMENT_ID", raising=False)
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "test"


def test_fallback_profile_is_dev(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    for key in (
        "AGENT_RUNTIME_PROFILE",
        "ENVIRONMENT",
        "APP_ENV",
        "NODE_ENV",
        "AGENT_HOSTED",
        "LANGGRAPH_DEPLOYMENT_ID",
        "CI",
        "GITHUB_ACTIONS",
    ):
        monkeypatch.delenv(key, raising=False)

    assert resolve_runtime_profile() == "dev"


def test_default_terminal_env_by_profile(monkeypatch):
    from agent_core.permissions.profiles import default_terminal_env

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    assert default_terminal_env("dev") == "local"
    assert default_terminal_env("test") == "local"
    assert default_terminal_env("hosted") == "docker"
    assert default_terminal_env("prod") == "docker"


def test_explicit_terminal_env_wins(monkeypatch):
    from agent_core.permissions.profiles import resolve_terminal_env

    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    assert resolve_terminal_env("prod") == "ssh"
