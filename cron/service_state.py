from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from cron.paths import atomic_write_json, ensure_cron_dirs, get_cron_dir


def service_status_path() -> Path:
    ensure_cron_dirs()
    return get_cron_dir() / "status.json"


def write_service_status(status: dict[str, Any], *, path: Path | None = None) -> None:
    target_path = path or service_status_path()
    ensure_cron_dirs()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target_path, status)


def read_service_status(*, path: Path | None = None) -> dict[str, Any] | None:
    target_path = path or service_status_path()
    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def is_status_fresh(
    status: dict[str, Any] | None,
    *,
    now_text: str,
    stale_after_seconds: int,
) -> bool:
    if not status:
        return False

    heartbeat_text = status.get("last_heartbeat_at")
    if not isinstance(heartbeat_text, str):
        return False

    try:
        heartbeat_at = _parse_iso_datetime(heartbeat_text)
        now = _parse_iso_datetime(now_text)
        age_seconds = (now - heartbeat_at).total_seconds()
    except (TypeError, ValueError):
        return False

    return age_seconds <= max(1, int(stale_after_seconds))


def _parse_iso_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value)
