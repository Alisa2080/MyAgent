from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DockerRuntimeDiagnostic:
    expected: bool
    env_type: str
    command_path: str | None
    version_ok: bool
    error: str | None = None
    suggestion: str | None = None


def _resolve_terminal_env(profile: str) -> str:
    try:
        from agent_core.permissions.profiles import default_terminal_env

        return default_terminal_env(profile)
    except Exception:
        import os

        return os.getenv("TERMINAL_ENV") or ("docker" if profile in {"prod", "hosted"} else "local")


def inspect_docker_runtime(profile: str) -> DockerRuntimeDiagnostic:
    env_type = _resolve_terminal_env(profile)
    expected = env_type == "docker"
    if not expected:
        return DockerRuntimeDiagnostic(expected=False, env_type=env_type, command_path=None, version_ok=True)

    command_path = shutil.which("docker")
    if command_path is None:
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=None,
            version_ok=False,
            error="docker command not found",
            suggestion="Install Docker or set TERMINAL_ENV=local for development.",
        )

    try:
        completed = subprocess.run(
            [command_path, "version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=command_path,
            version_ok=False,
            error=f"docker version failed: {exc}",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "docker version failed").strip().splitlines()[0]
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=command_path,
            version_ok=False,
            error=f"Docker command is available but 'docker version' failed: {message}",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        )
    return DockerRuntimeDiagnostic(expected=True, env_type=env_type, command_path=command_path, version_ok=True)
