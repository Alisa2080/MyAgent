from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cron.paths import atomic_replace, secure_dir, secure_file
from cron.service_context import get_service_env_file

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SENSITIVE_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "KEY")
_SENSITIVE_KEYS = frozenset({"FEISHU_APP_ID"})


@dataclass(frozen=True)
class ServiceEnvFileStatus:
    path: Path
    exists: bool
    readable: bool
    permissions_ok: bool
    error: str | None = None


def validate_service_env_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not _KEY_RE.match(normalized):
        raise ValueError(f"invalid service env key: {key!r}")
    return normalized


def validate_service_env_value(value: str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("service env values must be single-line")
    return text


def read_service_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if _KEY_RE.match(key):
            result[key] = value.strip()
    return result


def write_service_env(values: dict[str, str], path: Path | None = None) -> Path:
    env_path = path or get_service_env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    secure_dir(env_path.parent)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(env_path.parent),
        prefix=f".{env_path.name}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key in sorted(values):
                normalized_key = validate_service_env_key(key)
                normalized_value = validate_service_env_value(values[key])
                handle.write(f"{normalized_key}={normalized_value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, env_path)
        secure_file(env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return env_path


def set_service_env(key: str, value: str) -> Path:
    values = read_service_env()
    values[validate_service_env_key(key)] = validate_service_env_value(value)
    return write_service_env(values)


def unset_service_env(key: str) -> bool:
    normalized = validate_service_env_key(key)
    values = read_service_env()
    existed = normalized in values
    values.pop(normalized, None)
    write_service_env(values)
    return existed


def is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    if key.upper() in _SENSITIVE_KEYS:
        return True
    return any(marker in upper for marker in _SENSITIVE_MARKERS)


def mask_value(key: str, value: str) -> str:
    if is_sensitive_key(key):
        return "********"
    return value


def masked_service_env() -> dict[str, str]:
    return {key: mask_value(key, value) for key, value in read_service_env().items()}


def inspect_service_env_file(path: Path | None = None) -> ServiceEnvFileStatus:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return ServiceEnvFileStatus(env_path, exists=False, readable=False, permissions_ok=True)
    try:
        env_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ServiceEnvFileStatus(env_path, exists=True, readable=False, permissions_ok=False, error=str(exc))
    permissions_ok = True
    if os.name != "nt":
        permissions_ok = (env_path.stat().st_mode & 0o077) == 0
    return ServiceEnvFileStatus(env_path, exists=True, readable=True, permissions_ok=permissions_ok)
