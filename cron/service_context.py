from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cron.paths import get_cron_dir


@dataclass(frozen=True)
class ServiceRuntimeContext:
    project_root: Path | None
    working_directory: Path | None
    pythonpath: str | None
    service_env_file: Path


def detect_project_root(start: str | Path | None = None) -> Path | None:
    if start is None:
        start_path = Path(__file__).resolve()
    else:
        start_path = Path(start).resolve()
    current = start_path.parent if start_path.is_file() else start_path
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    if (Path.cwd() / "pyproject.toml").exists() and (Path.cwd() / "agent_cli").exists():
        return Path.cwd().resolve()
    if (Path.cwd() / "agent_cli").exists() and (Path.cwd() / "cron").exists():
        return Path.cwd().resolve()
    return None


def compose_pythonpath(project_root: Path | None, existing: str | None = None) -> str | None:
    parts: list[str] = []
    if project_root is not None:
        parts.append(str(project_root))
    existing_value = os.getenv("PYTHONPATH") if existing is None else existing
    if existing_value:
        parts.extend(part for part in existing_value.split(os.pathsep) if part)
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return os.pathsep.join(deduped) or None


def get_service_env_file() -> Path:
    return (get_cron_dir() / "service.env").resolve()


def build_service_runtime_context(*, module_path: str | Path | None = None) -> ServiceRuntimeContext:
    project_root = detect_project_root(module_path)
    return ServiceRuntimeContext(
        project_root=project_root,
        working_directory=project_root,
        pythonpath=compose_pythonpath(project_root),
        service_env_file=get_service_env_file(),
    )


@dataclass(frozen=True)
class InstalledServiceContextStatus:
    installed: bool
    working_directory_ok: bool
    pythonpath_ok: bool
    service_env_linked: bool
    detail: str | None = None


def inspect_text_service_context(
    text: str,
    *,
    project_root: Path | None,
    service_env_file: Path,
    platform: str,
) -> InstalledServiceContextStatus:
    if not text:
        return InstalledServiceContextStatus(False, False, False, False, "service definition missing")
    expected_root = "" if project_root is None else str(project_root)
    working_ok = bool(expected_root and expected_root in text and "WorkingDirectory" in text)
    pythonpath_ok = bool(expected_root and "PYTHONPATH" in text and expected_root in text)
    if platform == "systemd-user":
        env_linked = f"EnvironmentFile=-{service_env_file}" in text
    else:
        env_linked = True
    return InstalledServiceContextStatus(True, working_ok, pythonpath_ok, env_linked)
