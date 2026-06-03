from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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