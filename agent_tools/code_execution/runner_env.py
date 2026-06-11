from __future__ import annotations

import os

from agent_tools.code_execution.config import CodeExecutionConfig


SAFE_ENV_EXACT = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
})
SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH")


def _is_safe_env_name(name: str) -> bool:
    upper = name.upper()
    if any(marker in upper for marker in SECRET_SUBSTRINGS):
        return False
    return name in SAFE_ENV_EXACT or any(name.startswith(prefix) for prefix in SAFE_ENV_PREFIXES)


def safe_child_env(config: CodeExecutionConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if _is_safe_env_name(name) and config.env_name_allowed(name)
    }
    for name in config.env_allowlist:
        if config.env_name_allowed(name) and name in os.environ:
            env[name] = os.environ[name]
    env.update(extra or {})
    return env


def _join_roots(*roots: str | None) -> str:
    return os.pathsep.join(str(root) for root in roots if root)
