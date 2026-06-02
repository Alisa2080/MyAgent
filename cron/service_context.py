from __future__ import annotations

import os
import plistlib
import shlex
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
    stale_service_env_keys: tuple[str, ...] = ()


def _directive_values(text: str, name: str) -> list[str]:
    prefix = f"{name}="
    values: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        values.append(line[len(prefix) :].strip())
    return values


def _split_systemd_words(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return [value.strip().strip('"')]


def _unquote_systemd_value(value: str) -> str:
    words = _split_systemd_words(value)
    return words[0] if words else ""


def _pythonpath_contains(value: str | None, project_root: Path | None) -> bool:
    if value is None or project_root is None:
        return False
    return str(project_root) in [part for part in value.split(os.pathsep) if part]


def _systemd_environment(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in _directive_values(text, "Environment"):
        for word in _split_systemd_words(value):
            if "=" not in word:
                continue
            key, item_value = word.split("=", 1)
            env[key] = item_value
    return env


def _systemd_environment_files(text: str) -> list[str]:
    files: list[str] = []
    for value in _directive_values(text, "EnvironmentFile"):
        optional = value.startswith("-")
        path_value = value[1:] if optional else value
        files.append(_unquote_systemd_value(path_value))
    return files


def inspect_text_service_context(
    text: str,
    *,
    project_root: Path | None,
    service_env_file: Path,
    platform: str,
) -> InstalledServiceContextStatus:
    if not text:
        return InstalledServiceContextStatus(False, False, False, False, "service definition missing")
    if platform == "systemd-user":
        working_directories = [
            _unquote_systemd_value(value)
            for value in _directive_values(text, "WorkingDirectory")
        ]
        environment = _systemd_environment(text)
        working_ok = bool(project_root and str(project_root) in working_directories)
        pythonpath_ok = _pythonpath_contains(environment.get("PYTHONPATH"), project_root)
        env_linked = str(service_env_file) in _systemd_environment_files(text)
    else:
        expected_root = "" if project_root is None else str(project_root)
        working_ok = bool(expected_root and expected_root in text and "WorkingDirectory" in text)
        pythonpath_ok = bool(expected_root and "PYTHONPATH" in text and expected_root in text)
        env_linked = True
    return InstalledServiceContextStatus(True, working_ok, pythonpath_ok, env_linked)


def inspect_launchd_service_context(
    payload: bytes,
    *,
    project_root: Path | None,
    service_env_values: dict[str, str] | None = None,
) -> InstalledServiceContextStatus:
    if not payload:
        return InstalledServiceContextStatus(False, False, False, False, "service definition missing")
    try:
        plist = plistlib.loads(payload)
    except Exception as exc:
        return InstalledServiceContextStatus(False, False, False, False, str(exc))
    env = plist.get("EnvironmentVariables")
    if not isinstance(env, dict):
        env = {}
    working_ok = bool(
        project_root is not None
        and str(plist.get("WorkingDirectory") or "") == str(project_root)
    )
    pythonpath_ok = _pythonpath_contains(str(env.get("PYTHONPATH") or ""), project_root)
    stale_keys = tuple(
        sorted(
            key
            for key, value in (service_env_values or {}).items()
            if str(env.get(key) or "") != value
        )
    )
    return InstalledServiceContextStatus(
        True,
        working_ok,
        pythonpath_ok,
        service_env_linked=not stale_keys,
        stale_service_env_keys=stale_keys,
    )
