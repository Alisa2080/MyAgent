from __future__ import annotations

import importlib
import os
import platform
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_cli.config import ConfigError, settings_from_config
from agent_cli.paths import get_cli_home, get_db_path
from agent_cli.session_store import SessionStore


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
        with store.connect() as conn:
            ownerless = conn.execute(
                "SELECT COUNT(*) FROM cli_background_tasks "
                "WHERE status IN ('queued', 'running', 'waiting_approval', 'completing', 'stopping') "
                "AND owner_id IS NULL"
            ).fetchone()[0]
    except Exception as exc:
        return fail("Background Tasks", f"cannot inspect background task metadata: {exc}")
    if ownerless:
        return warn("Background Tasks", f"{ownerless} active task(s) have no owner metadata")
    if stale:
        return warn("Background Tasks", f"{len(stale)} active task(s) look stale")
    return ok("Background Tasks", "metadata readable")


def run_health_checks(
    workdir: str, tmp_path: Path | None = None, cli_home: Path | None = None
) -> list[HealthCheck]:
    if cli_home is None:
        cli_home = get_cli_home()
    db_path = get_db_path()
    cwd = Path.cwd()

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
