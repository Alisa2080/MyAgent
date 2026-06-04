from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from agent_cli.paths import ensure_cli_home
from cron.paths import atomic_replace, secure_dir, secure_file


_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def get_service_env_file() -> Path:
    return ensure_cli_home() / "gateway" / "service.env"


def validate_service_env_key(key: str) -> str:
    value = str(key).strip()
    if not _KEY_RE.match(value):
        raise ValueError(f"invalid service env key: {key!r}")
    return value


def validate_service_env_value(value: str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("service env values must be single-line")
    return text


def read_service_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return {}
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


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
                handle.write(f"{validate_service_env_key(key)}={validate_service_env_value(values[key])}\n")
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


def set_service_env(key: str, value: str, path: Path | None = None) -> Path:
    values = read_service_env(path=path)
    values[validate_service_env_key(key)] = validate_service_env_value(value)
    return write_service_env(values, path=path)


def unset_service_env(key: str, path: Path | None = None) -> bool:
    values = read_service_env(path=path)
    normalized = validate_service_env_key(key)
    existed = normalized in values
    values.pop(normalized, None)
    write_service_env(values, path=path)
    return existed


def masked_service_env(path: Path | None = None) -> dict[str, str]:
    result = {}
    for key, value in read_service_env(path=path).items():
        result[key] = value if len(value) <= 4 else value[:2] + "***" + value[-2:]
    return result
