from __future__ import annotations

import importlib
import os
import platform
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from contextlib import contextmanager

from agent_cli.config import ConfigError, settings_from_config
from agent_cli.paths import get_cli_home
from gateway.service_env import read_service_env as read_gateway_service_env
from cron.service_env import read_service_env as read_cron_service_env

from cron.service_manager import compose_service_status
from cron.delivery_registry import default_delivery_registry
from gateway.registry import default_gateway_registry
from gateway.service_state import gateway_status_transport, read_gateway_status
from cron.delivery_store import DeliveryStore
from gateway.inbox_store import GatewayInboxStore


DoctorStatus = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: DoctorStatus
    message: str


def ok(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "OK", message)


def warn(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "WARN", message)


def fail(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "FAIL", message)


@contextmanager
def _merged_env(service_values: dict[str, str]):
    original = {key: os.environ.get(key) for key in service_values}
    try:
        for key, value in service_values.items():
            if key not in os.environ:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_python_version() -> HealthCheck:
    version = sys.version_info
    message = f"Python {version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        return ok("Python Version", message)
    return fail("Python Version", f"{message}; Python >= 3.11 is required")


def check_platform() -> HealthCheck:
    return ok("Platform", platform.platform())


def check_dependencies() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    imports = [
        ("prompt_toolkit", "prompt_toolkit"),
        ("langchain", "langchain"),
        ("langgraph", "langgraph"),
        ("langgraph-checkpoint-sqlite", "langgraph.checkpoint.sqlite"),
    ]
    for display, module_name in imports:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            checks.append(fail(display, f"missing import {module_name}: {exc}"))
        else:
            checks.append(ok(display, "available"))
    return checks


def check_openai_api_key() -> HealthCheck:
    if os.getenv("OPENAI_API_KEY"):
        return ok("OPENAI_API_KEY", "set")
    return warn("OPENAI_API_KEY", "not set")


def check_workdir(workdir: str) -> HealthCheck:
    path = Path(workdir)
    if not path.exists():
        return fail("Workdir", f"does not exist: {workdir}")
    if not path.is_dir():
        return fail("Workdir", f"not a directory: {workdir}")
    return ok("Workdir", str(path))


def _write_probe(directory: Path, filename: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / filename
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def check_cli_home(cli_home: Path) -> HealthCheck:
    try:
        _write_probe(cli_home, ".doctor-write-test")
    except Exception as exc:
        return fail("CLI Home", f"not writable at {cli_home}: {exc}")
    return ok("CLI Home", str(cli_home))


def check_sqlite_db(db_path: Path) -> HealthCheck:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS doctor_probe (id INTEGER)")
            conn.execute("DROP TABLE doctor_probe")
    except Exception as exc:
        return fail("SQLite DB", f"cannot open {db_path}: {exc}")
    return ok("SQLite DB", str(db_path))


def check_logs(cli_home: Path) -> HealthCheck:
    logs_dir = cli_home / "logs"
    try:
        _write_probe(logs_dir, ".doctor-write-test")
    except Exception as exc:
        return fail("Logs", f"not writable at {logs_dir}: {exc}")
    return ok("Logs", str(logs_dir))


def check_config(cli_home: Path) -> HealthCheck:
    config_path = cli_home / "config.yaml"
    if not config_path.exists():
        return ok("Config", f"optional file missing: {config_path}")
    try:
        settings_from_config(cli_home=cli_home, profile=None, cli_model=None)
        return ok("Config", f"config readable: {config_path}")
    except ConfigError as exc:
        return fail("Config", str(exc))
    except Exception as exc:
        return fail("Config", f"cannot read {config_path}: {exc}")


def check_dotenv(cli_home: Path, cwd: Path) -> HealthCheck:
    env_paths = [cli_home / ".env", cwd / ".env"]
    existing = [path for path in env_paths if path.exists()]
    if not existing:
        return ok("Dotenv", f"no .env files found; checked {env_paths[0]} and {env_paths[1]}")
    try:
        for path in existing:
            path.read_text(encoding="utf-8")
    except Exception as exc:
        return fail("Dotenv", f"dotenv file cannot be read: {exc}")
    return ok("Dotenv", "visible .env files: " + ", ".join(str(path) for path in existing))


def check_background_tasks(db_path: Path) -> HealthCheck:
    try:
        from agent_cli.background import ACTIVE_TASK_STATUSES, BackgroundTaskStore

        store = BackgroundTaskStore(db_path)
        rows = store.list_tasks(statuses=ACTIVE_TASK_STATUSES, limit=200)
        stale = [row for row in rows if getattr(row, "cancel_requested", False)]
        ownerless = 0
        dead_owner_ids: set[str] = set()
        with store.connect() as conn:
            ownerless = conn.execute(
                "SELECT COUNT(*) FROM cli_background_tasks "
                "WHERE status IN ('queued', 'running', 'waiting_approval', 'completing', 'stopping') "
                "AND owner_id IS NULL"
            ).fetchone()[0]
            owner_rows = conn.execute(
                "SELECT DISTINCT t.owner_id, o.process_id "
                "FROM cli_background_tasks t "
                "LEFT JOIN cli_background_owners o ON t.owner_id = o.owner_id "
                "WHERE t.status IN ('queued', 'running', 'waiting_approval', 'completing', 'stopping') "
                "AND t.owner_id IS NOT NULL"
            ).fetchall()
        for owner_id, process_id in owner_rows:
            if process_id is None or not BackgroundTaskStore._process_is_running(int(process_id)):
                dead_owner_ids.add(str(owner_id))
    except Exception as exc:
        return fail("Background Tasks", f"cannot inspect background task metadata: {exc}")
    if ownerless:
        return warn("Background Tasks", f"{ownerless} active task(s) have no owner metadata")
    if dead_owner_ids:
        return warn(
            "Background Tasks",
            f"{len(dead_owner_ids)} active owner(s) are dead or stale",
        )
    if stale:
        return warn("Background Tasks", f"{len(stale)} active task(s) look stale")
    return ok("Background Tasks", "metadata readable")


def _gateway_service_env_path(cli_home: Path) -> Path:
    return cli_home / "gateway" / "service.env"


def _read_gateway_service_env(cli_home: Path | None = None) -> dict[str, str]:
    path = _gateway_service_env_path(cli_home) if cli_home is not None else None
    return read_gateway_service_env(path=path)


def check_feishu_gateway_config(
    env: dict[str, str] | None = None,
    *,
    transport: str | None = None,
    cli_home: Path | None = None,
) -> list[str]:
    values = os.environ if env is None else env
    gateway_service_env = _read_gateway_service_env(cli_home) if env is None or cli_home is not None else {}
    required = ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    if transport in {None, "http", "feishu-http", "callback-http"}:
        required.append("FEISHU_CALLBACK_TOKEN")
    missing = [
        name
        for name in required
        if not values.get(name) and not gateway_service_env.get(name)
    ]
    if not missing:
        return []
    return [f"missing Feishu gateway environment variables: {', '.join(missing)}"]


def check_feishu_gateway(cli_home: Path) -> HealthCheck:
    gateway_status = read_gateway_status(cli_home)
    transport = gateway_status_transport(gateway_status)
    errors = check_feishu_gateway_config(transport=transport, cli_home=cli_home)
    if errors:
        return warn("Feishu Gateway", "; ".join(errors))
    return ok("Feishu Gateway", "configured")


def check_feishu_ws_gateway_config(cli_home: Path | None = None) -> HealthCheck:
    import os as _os

    gateway_service_env = _read_gateway_service_env(cli_home)
    missing = [
        key
        for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        if not _os.getenv(key) and not gateway_service_env.get(key)
    ]
    if missing:
        return warn("Feishu WebSocket", f"missing: {', '.join(missing)}")
    try:
        import lark_oapi  # noqa: F401
    except ModuleNotFoundError:
        return warn("Feishu WebSocket", "missing dependency: lark-oapi")
    return ok("Feishu WebSocket", "configured")


def check_feishu_ws_gateway(cli_home: Path | None = None) -> HealthCheck:
    errors = check_feishu_ws_gateway_config(cli_home)
    if errors.status == "WARN":
        return errors
    return ok("Feishu WebSocket", "configured")


def check_gateway_service() -> HealthCheck:
    try:
        from gateway import service_manager

        result = service_manager.service_status()
    except Exception as exc:
        return warn("Gateway Service", f"check failed: {exc}")
    if result.exit_code == 0:
        return ok("Gateway Service", result.message)
    return warn("Gateway Service", result.message)


def _format_counts(stats: dict[str, int]) -> str:
    """Format a stats dict into a compact key=value string."""
    if not stats:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(stats.items())]
    return " ".join(parts)


def check_cron_service() -> HealthCheck:
    try:
        status = compose_service_status()
    except Exception as exc:
        return warn("Cron Service", f"check failed: {exc}")
    details = [
        f"platform={getattr(status, 'platform', '-')}",
        f"active={getattr(status, 'active', None)}",
        f"enabled={getattr(status, 'enabled', None)}",
        f"fresh={getattr(status, 'heartbeat_fresh', None)}",
        f"state={getattr(status, 'process_state', None) or '-'}",
        f"leader={getattr(status, 'leader_state', None) or '-'}",
    ]
    if getattr(status, "last_heartbeat_at", None):
        details.append(f"heartbeat={status.last_heartbeat_at}")
    if getattr(status, "last_tick", None):
        details.append(f"last_tick={status.last_tick}")
    if getattr(status, "last_error", None):
        details.append(f"error={status.last_error}")
    detail_text = "; ".join(details)
    if not status.supported:
        return warn("Cron Service", f"unsupported platform; use `agent cron serve`; {detail_text}")
    if not status.installed:
        return warn("Cron Service", f"not installed; run `agent cron service install`; {detail_text}")
    if not status.active:
        return warn("Cron Service", f"installed but inactive; run `agent cron service start`; {detail_text}")
    if not status.enabled:
        return warn("Cron Service", f"installed but disabled; run `agent cron service install --force`; {detail_text}")
    if not status.heartbeat_fresh:
        return warn("Cron Service", f"heartbeat stale; {detail_text}")
    if status.process_state != "running":
        return warn("Cron Service", f"process state is not running; {detail_text}")
    return ok("Cron Service", f"running; {detail_text}")


def check_feishu_token(cli_home: Path | None = None) -> HealthCheck:
    gateway_service_env = _read_gateway_service_env(cli_home)
    missing = [
        k
        for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        if not os.environ.get(k) and not gateway_service_env.get(k)
    ]
    if missing:
        return warn("Feishu Token", f"missing env vars: {', '.join(missing)}")
    try:
        registry = default_gateway_registry()
        adapter = registry.get("feishu")
    except Exception as exc:
        return warn("Feishu Token", f"cannot load gateway registry: {exc}")
    if adapter is None:
        return warn("Feishu Token", "feishu gateway adapter not available")
    try:
        with _merged_env(gateway_service_env):
            result = adapter.token_smoke()
    except Exception as exc:
        return warn("Feishu Token", f"token smoke failed: {exc}")
    if not getattr(result, "ok", False):
        return warn("Feishu Token", f"token smoke failed: {getattr(result, 'error', 'unknown')}")
    return ok("Feishu Token", "token smoke passed")


def check_cron_feishu_delivery() -> HealthCheck:
    cron_service_env = read_cron_service_env()
    missing_service_env = [
        key for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not cron_service_env.get(key)
    ]
    if missing_service_env:
        return warn(
            "Cron Feishu Delivery",
            "cron service env missing required Feishu variables: " + ", ".join(missing_service_env),
        )
    try:
        cron_reg = default_delivery_registry()
        gw_reg = default_gateway_registry()
    except Exception as exc:
        return warn("Cron Feishu Delivery", f"cannot load registries: {exc}")
    active_keys = set(cron_reg.active_adapter_keys()) if hasattr(cron_reg, "active_adapter_keys") else set()
    adapter_keys = set(cron_reg.adapter_keys()) if hasattr(cron_reg, "adapter_keys") else active_keys
    missing = []
    if "origin" not in active_keys:
        missing.append("active origin delivery adapter")
    if "feishu" not in active_keys:
        missing.append("active feishu delivery adapter")
    if "feishu" not in adapter_keys or cron_reg.get("feishu") is None:
        missing.append("feishu adapter in cron delivery registry")
    gw_adapter = gw_reg.get("feishu")
    if gw_adapter is None:
        return warn("Cron Feishu Delivery", "feishu adapter missing from gateway registry")
    if missing:
        return warn("Cron Feishu Delivery", "missing: " + ", ".join(missing))
    try:
        from gateway.contracts import PlatformMessageTarget

        with _merged_env(cron_service_env):
            validation = gw_adapter.validate_target(
                PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="doctor-probe")
            )
    except Exception as exc:
        return warn("Cron Feishu Delivery", f"target validation failed: {exc}")
    if not validation.ok:
        return warn("Cron Feishu Delivery", f"target validation failed: {validation.error or 'unknown'}")
    return ok("Cron Feishu Delivery", "active adapters: " + ", ".join(sorted(active_keys)))


def check_gateway_inbox(cli_home: Path) -> HealthCheck:
    inbox_path = cli_home / "gateway" / "gateway.sqlite"
    if not inbox_path.exists():
        return ok("Gateway Inbox", "no gateway inbox found")
    try:
        store = GatewayInboxStore(inbox_path)
        stats = store.stats()
    except Exception as exc:
        return warn("Gateway Inbox", f"cannot read inbox: {exc}")
    count_str = _format_counts(stats)
    if stats.get("failed", 0) > 0 or stats.get("dead", 0) > 0:
        return warn("Gateway Inbox", f"inbox: {count_str}")
    return ok("Gateway Inbox", f"inbox: {count_str}")


def check_cron_delivery_queue(*, cron_service_healthy: bool | None = None) -> HealthCheck:
    try:
        store = DeliveryStore()
        stats = store.stats()
        recent_errors = store.recent_errors(limit=2)
    except Exception as exc:
        return warn("Cron Delivery Queue", f"cannot read delivery queue: {exc}")
    count_str = _format_counts(stats)
    latest_error = next((row for row in recent_errors if row.get("last_error")), None)
    if stats.get("pending", 0) > 0 and cron_service_healthy is False:
        return warn(
            "Cron Delivery Queue",
            f"delivery: {count_str}; pending events may not dispatch because cron service is not healthy",
        )
    if stats.get("failed", 0) > 0 or stats.get("dead", 0) > 0:
        if latest_error:
            return warn(
                "Cron Delivery Queue",
                f"delivery: {count_str}; latest_error={latest_error.get('adapter_key') or '-'} {latest_error.get('status') or '-'} {latest_error.get('last_error') or '-'}",
            )
        return warn("Cron Delivery Queue", f"delivery: {count_str}")
    return ok("Cron Delivery Queue", f"delivery: {count_str}")


def run_health_checks(workdir: str, cli_home: Path | None = None) -> list[HealthCheck]:
    if cli_home is None:
        cli_home = get_cli_home()
    db_path = cli_home / "cli.sqlite"
    cwd = Path.cwd()

    cron_service = check_cron_service()
    results: list[HealthCheck] = [
        check_python_version(),
        check_platform(),
        *check_dependencies(),
        check_workdir(workdir),
        check_cli_home(cli_home),
        check_sqlite_db(db_path),
        check_logs(cli_home),
        check_config(cli_home),
        check_dotenv(cli_home, cwd),
        check_openai_api_key(),
        check_background_tasks(db_path),
        check_feishu_gateway(cli_home),
        check_feishu_ws_gateway(cli_home),
        check_gateway_service(),
        cron_service,
        check_feishu_token(cli_home),
        check_cron_feishu_delivery(),
        check_gateway_inbox(cli_home),
        check_cron_delivery_queue(cron_service_healthy=cron_service.status == "OK"),
    ]
    return results


def doctor_exit_code(results: list[HealthCheck]) -> int:
    return 1 if any(result.status == "FAIL" for result in results) else 0


def render_doctor_output(results: list[HealthCheck]) -> str:
    lines = ["=== CLI Health Check ===", ""]
    name_width = max([len(result.name) for result in results] + [4])
    for result in results:
        lines.append(f"{result.status:<5} {result.name:<{name_width}}  {result.message}")
    return "\n".join(lines)
