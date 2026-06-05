from __future__ import annotations

import os
import threading
from pathlib import Path

from cron.service_manager import compose_service_status
from gateway.callback_server import CallbackApplication, serve_callback_http
from gateway.transports.feishu_ws import serve_feishu_ws_gateway, validate_feishu_ws_env


_STATUS_STALE_SECONDS = 120
_HEARTBEAT_INTERVAL_SECONDS = 30


def gateway_handlers():
    return {"gateway": handle_gateway}


def _default_home() -> Path:
    from agent_cli.paths import ensure_cli_home

    return ensure_cli_home()


def _gateway_status_lines(*, home: str | Path | None = None) -> tuple[int, list[str]]:
    from gateway.service_state import (
        gateway_status_transport,
        is_gateway_status_running,
        read_gateway_status,
    )

    home_path = Path(home) if home is not None else _default_home()
    status = read_gateway_status(home_path)
    if not is_gateway_status_running(status):
        return 1, ["Gateway service is not running"]
    platforms = ", ".join(status.get("platforms") or []) or "-"
    transport = gateway_status_transport(status) or "-"
    lines = [
        "Gateway service is running",
        f"Platforms: {platforms}",
        f"Transport: {transport}",
        f"Updated: {status.get('updated_at')}",
    ]
    inbox_line = _inbox_summary_line(home_path)
    if inbox_line:
        lines.append(inbox_line)
    return 0, lines


def _inbox_summary_line(home: Path) -> str | None:
    from gateway.inbox_store import GatewayInboxStore

    path = home / "gateway" / "gateway.sqlite"
    if not path.exists():
        return None
    stats = GatewayInboxStore(path).stats()
    return (
        "Inbox: "
        f"pending={stats.get('pending', 0)} "
        f"processing={stats.get('processing', 0)} "
        f"failed={stats.get('failed', 0)} "
        f"dead={stats.get('dead', 0)} "
        f"succeeded={stats.get('succeeded', 0)}"
    )


def gateway_status(*, home: str | Path | None = None) -> int:
    exit_code, lines = _gateway_status_lines(home=home)
    for line in lines:
        print(line)
    return exit_code


def gateway_feishu_ws(args, *, home: str | Path | None = None) -> int:
    from gateway.service import GatewayService

    missing = validate_feishu_ws_env()
    if missing:
        print(f"Missing required Feishu environment variables: {', '.join(missing)}")
        return 2
    target_home = Path(home) if home is not None else _default_home()
    if _refuse_if_other_transport(home=target_home, desired="feishu-ws"):
        return 2
    _warn_if_cron_service_not_running(home=target_home)
    service = GatewayService(home=target_home)
    service.write_status(process_state="running", transport="feishu-ws")
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_status_heartbeat,
        args=(service, stop_heartbeat, "feishu-ws"),
        daemon=True,
    )
    heartbeat.start()
    print("Gateway service listening with transport feishu-ws")
    try:
        serve_feishu_ws_gateway(home=target_home)
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=1)
        service.write_status(process_state="exited", transport="feishu-ws")
    return 0


def gateway_serve(args, *, home: str | Path | None = None) -> int:
    from gateway.service import GatewayService

    target_home = Path(home) if home is not None else _default_home()
    if _refuse_if_other_transport(home=target_home, desired="http"):
        return 2
    service = GatewayService(home=target_home)
    service.write_status(process_state="running", transport="http")
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_status_heartbeat,
        args=(service, stop_heartbeat, "http"),
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
        service.write_status(process_state="exited", transport="http")
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


def _refuse_if_other_transport(*, home: Path, desired: str) -> bool:
    from gateway.service_state import (
        gateway_status_transport,
        is_gateway_status_running,
        read_gateway_status,
    )

    status = read_gateway_status(home)
    if not is_gateway_status_running(status, stale_after_seconds=_STATUS_STALE_SECONDS):
        return False
    running = gateway_status_transport(status) or "unknown"
    if running != desired:
        print(f"Gateway service is already running with transport {running}")
        return True
    return False


def _warn_if_cron_service_not_running(*, home: Path) -> None:
    try:
        service_status = compose_service_status()
    except Exception:
        print("WARN Cron service status could not be checked; run `python3 -m agent_cli.main cron service status` for details")
        print("   scheduled cron jobs will not run automatically")
        return

    if service_status.supported:
        healthy = (
            service_status.active
            and service_status.heartbeat_fresh
            and service_status.process_state == "running"
        )
        if healthy:
            return
        print("WARN Cron service is not running")
        print("   scheduled cron jobs will not run automatically")
        print("   To start the cron scheduler, run: python3 -m agent_cli.main cron service start")
    else:
        print("WARN Cron service is not available on this platform")
        print("   scheduled cron jobs will not run automatically")
        print("   To start the cron scheduler in the foreground, run: python3 -m agent_cli.main cron serve")


def _status_heartbeat(service, stop_event: threading.Event, transport: str) -> None:
    interval = _heartbeat_interval_seconds()
    while not stop_event.wait(interval):
        service.write_status(process_state="running", transport=transport)


def _heartbeat_interval_seconds() -> float:
    raw = os.getenv("AGENT_GATEWAY_HEARTBEAT_SECONDS")
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return _HEARTBEAT_INTERVAL_SECONDS


def handle_gateway_service(args, subcommand):
    from gateway import service_manager

    if subcommand == "install":
        gateway_result = service_manager.install_service(
            transport=getattr(args, "transport", "feishu-ws"),
            force=bool(getattr(args, "force", False)),
        )
        if not bool(getattr(args, "with_cron", False)):
            return _print_result(gateway_result)
        if gateway_result.exit_code != 0:
            return _print_result(gateway_result)

        from cron import service_manager as cron_service_manager

        cron_result = cron_service_manager.install_service(
            interval_seconds=60,
            lease_seconds=180,
            force=bool(getattr(args, "force", False)),
        )
        print(gateway_result.message)
        print(cron_result.message)
        return cron_result.exit_code
    if subcommand == "start":
        return _print_result(service_manager.start_service())
    if subcommand == "stop":
        return _print_result(service_manager.stop_service())
    if subcommand == "restart":
        return _print_result(service_manager.restart_service())
    if subcommand == "status":
        return _print_result(service_manager.service_status())
    if subcommand == "uninstall":
        return _print_result(service_manager.uninstall_service())
    if subcommand == "logs":
        return _print_result(service_manager.service_logs(lines=getattr(args, "lines", 100)))
    if subcommand == "env":
        svc_env_sub = getattr(args, "gateway_service_env_command", None)
        if svc_env_sub == "set":
            return _print_result(service_manager.service_env_set(args.key, args.value))
        if svc_env_sub == "unset":
            return _print_result(service_manager.service_env_unset(args.key))
        if svc_env_sub == "list":
            return _print_result(service_manager.service_env_list())
        return _print_result(service_manager.service_env_list())
    return _print_result(service_manager.service_status())


def _print_result(result):
    print(result.message)
    return result.exit_code
