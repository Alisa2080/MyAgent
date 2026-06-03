from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from gateway.callback_server import CallbackApplication, serve_callback_http


_STATUS_STALE_SECONDS = 120
_HEARTBEAT_INTERVAL_SECONDS = 30


def gateway_handlers():
    return {"gateway": handle_gateway}


def _default_home() -> Path:
    from agent_cli.paths import ensure_cli_home

    return ensure_cli_home()


def _gateway_status_lines(*, home: str | Path | None = None) -> tuple[int, list[str]]:
    from gateway.service_state import read_gateway_status

    status = read_gateway_status(Path(home) if home is not None else _default_home())
    if not _status_is_running(status):
        return 1, ["Gateway service is not running"]
    platforms = ", ".join(status.get("platforms") or []) or "-"
    return (
        0,
        [
            "Gateway service is running",
            f"Platforms: {platforms}",
            f"Updated: {status.get('updated_at')}",
        ],
    )


def gateway_status(*, home: str | Path | None = None) -> int:
    exit_code, lines = _gateway_status_lines(home=home)
    for line in lines:
        print(line)
    return exit_code


def gateway_serve(args, *, home: str | Path | None = None) -> int:
    from gateway.service import GatewayService

    service = GatewayService(home=Path(home) if home is not None else _default_home())
    service.write_status(process_state="running")
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_status_heartbeat,
        args=(service, stop_heartbeat),
        daemon=True,
    )
    heartbeat.start()
    app = CallbackApplication(registry=service.registry, dispatch=service.handle_event)
    host = str(getattr(args, "host", "127.0.0.1"))
    port = int(getattr(args, "port", 8765))
    print(f"Gateway service listening on {host}:{port}")
    try:
        serve_callback_http(app, host=host, port=port)
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=1)
        service.write_status(process_state="exited")
    return 0


def _usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  /gateway status",
        ]
    )


def handle_gateway(ctx, arg, command):
    subcommand = arg.strip().lower() if arg else "status"
    if subcommand in {"status", ""}:
        _exit_code, lines = _gateway_status_lines(home=ctx.effective_cli_home())
        return "\n".join(lines)
    return f"Unknown /gateway command: {subcommand}\n{_usage()}"


def _status_is_running(status: dict | None) -> bool:
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
    age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    return age <= _STATUS_STALE_SECONDS


def _status_heartbeat(service, stop_event: threading.Event) -> None:
    interval = _heartbeat_interval_seconds()
    while not stop_event.wait(interval):
        service.write_status(process_state="running")


def _heartbeat_interval_seconds() -> float:
    raw = os.getenv("AGENT_GATEWAY_HEARTBEAT_SECONDS")
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return _HEARTBEAT_INTERVAL_SECONDS
