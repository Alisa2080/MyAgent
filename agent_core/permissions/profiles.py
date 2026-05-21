from __future__ import annotations

import os

from agent_core.permissions.models import RuntimeProfile


VALID_PROFILES: set[str] = {"dev", "test", "hosted", "prod"}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_runtime_profile() -> RuntimeProfile:
    explicit = os.getenv("AGENT_RUNTIME_PROFILE", "").strip().lower()
    if explicit:
        if explicit not in VALID_PROFILES:
            raise ValueError(
                "Invalid AGENT_RUNTIME_PROFILE "
                f"{explicit!r}. Expected one of: dev, test, hosted, prod."
            )
        return explicit  # type: ignore[return-value]

    production_values = {
        os.getenv("ENVIRONMENT", "").strip().lower(),
        os.getenv("APP_ENV", "").strip().lower(),
        os.getenv("NODE_ENV", "").strip().lower(),
    }
    if production_values & {"prod", "production"}:
        return "prod"

    if _truthy_env("AGENT_HOSTED") or os.getenv("LANGGRAPH_DEPLOYMENT_ID"):
        return "hosted"

    if _truthy_env("CI") or _truthy_env("GITHUB_ACTIONS"):
        return "test"

    return "dev"


def default_terminal_env(profile: RuntimeProfile | str) -> str:
    return "docker" if profile in {"hosted", "prod"} else "local"


def resolve_terminal_env(profile: RuntimeProfile | str | None = None) -> str:
    explicit = os.getenv("TERMINAL_ENV", "").strip().lower()
    if explicit:
        return explicit
    return default_terminal_env(profile or resolve_runtime_profile())


def profile_enforces_docker_network(profile: RuntimeProfile | str | None = None) -> bool:
    return (profile or resolve_runtime_profile()) in {"hosted", "prod"}
