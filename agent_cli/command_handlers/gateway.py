from __future__ import annotations

from pathlib import Path


def _default_home() -> Path:
    from cron.paths import cron_home

    return cron_home()


def gateway_status(*, home: str | Path | None = None) -> int:
    from gateway.service_state import read_gateway_status

    status = read_gateway_status(Path(home) if home is not None else _default_home())
    if not status or status.get("process_state") != "running":
        print("Gateway service is not running")
        return 1
    platforms = ", ".join(status.get("platforms") or []) or "-"
    print("Gateway service is running")
    print(f"Platforms: {platforms}")
    print(f"Updated: {status.get('updated_at')}")
    return 0


def gateway_serve(args) -> int:
    from gateway.service import GatewayService

    service = GatewayService(home=_default_home())
    service.write_status(process_state="running")
    print("Gateway service foreground mode is ready")
    return 0