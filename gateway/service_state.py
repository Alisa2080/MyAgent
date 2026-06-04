from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_GATEWAY_STATUS_STALE_SECONDS = 120


def gateway_state_dir(home: str | Path) -> Path:
    return Path(home) / "gateway"


def gateway_status_path(home: str | Path) -> Path:
    return gateway_state_dir(home) / "status.json"


def write_gateway_status(home: str | Path, status: dict[str, Any]) -> None:
    path = gateway_status_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"service": "gateway", "updated_at": datetime.now(timezone.utc).isoformat(), **status}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_gateway_status(home: str | Path) -> dict[str, Any] | None:
    path = gateway_status_path(home)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def gateway_status_transport(status: dict[str, Any] | None) -> str | None:
    if not status:
        return None
    value = status.get("transport")
    return str(value) if value else None


def is_gateway_status_running(
    status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_GATEWAY_STATUS_STALE_SECONDS,
) -> bool:
    if not status or status.get("process_state") != "running":
        return False
    updated_at = status.get("updated_at")
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) - updated.astimezone(timezone.utc) <= timedelta(
        seconds=stale_after_seconds
    )
