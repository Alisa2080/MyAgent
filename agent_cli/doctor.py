from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_cli.config import ConfigError, load_config_file
from agent_cli.paths import get_cli_home
from agent_cli.session_store import SessionStore


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str
    message: str


def check_python_version() -> tuple[bool, str]:
    """Check Python version is >= 3.11."""
    version = sys.version_info
    ok = version >= (3, 11)
    return ok, f"Python {version.major}.{version.minor}.{version.micro}"


def check_platform() -> tuple[bool, str]:
    """Check platform info."""
    return True, platform.platform()


def check_dependencies() -> tuple[bool, str]:
    """Check required dependencies are importable."""
    deps = ["prompt_toolkit", "langgraph", "langchain"]
    missing = []
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)

    if missing:
        return False, f"Missing: {', '.join(missing)}"
    return True, "All dependencies available"


def check_session_store(tmp_path: Path | None = None) -> tuple[bool, str]:
    """Check session store can be created."""
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.gettempdir())

    try:
        store_path = tmp_path / f"test_{id(tmp_path)}.sqlite"
        store = SessionStore(store_path)
        record = store.create_session(workdir="/test", model=None, title="test")
        ok = record is not None
        store_path.unlink(missing_ok=True)
        return ok, "Session store working"
    except Exception as e:
        return False, f"Session store error: {e}"


def check_workdir(workdir: str) -> tuple[bool, str]:
    """Check workdir exists and is writable."""
    path = Path(workdir)
    if not path.exists():
        return False, f"Workdir does not exist: {workdir}"
    if not path.is_dir():
        return False, f"Workdir is not a directory: {workdir}"
    return True, f"Workdir: {workdir}"


def check_config(cli_home: Path) -> tuple[bool, str]:
    """Check config.yaml is readable if present."""
    config_path = cli_home / "config.yaml"
    if not config_path.exists():
        return True, f"optional file missing: {config_path}"
    try:
        load_config_file(config_path)
        return True, f"config readable: {config_path}"
    except ConfigError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Config cannot be read at {config_path}: {exc}"


def check_dotenv(cli_home: Path, cwd: Path) -> tuple[bool, str]:
    """Check dotenv files are readable if present."""
    env_paths = [cli_home / ".env", cwd / ".env"]
    existing = [str(p) for p in env_paths if p.exists()]
    if not existing:
        return True, f"no .env files found; checked {env_paths[0]} and {env_paths[1]}"
    try:
        for path in env_paths:
            if path.exists():
                path.read_text(encoding="utf-8")
        return True, "visible .env files: " + ", ".join(existing)
    except Exception as exc:
        return False, f"dotenv file cannot be read: {exc}"


def run_health_checks(
    workdir: str, tmp_path: Path | None = None, cli_home: Path | None = None
) -> list[HealthCheck]:
    """Run all health checks."""
    if cli_home is None:
        cli_home = get_cli_home()
    cwd = Path.cwd()

    checks = [
        ("Python Version", check_python_version),
        ("Platform", check_platform),
        ("Dependencies", check_dependencies),
        ("Session Store", lambda: check_session_store(tmp_path)),
        ("Workdir", lambda: check_workdir(workdir)),
        ("Config", lambda: check_config(cli_home)),
        ("Dotenv", lambda: check_dotenv(cli_home, cwd)),
    ]

    results = []
    for name, check_fn in checks:
        ok, message = check_fn()
        results.append(HealthCheck(name=name, status="OK" if ok else "FAIL", message=message))

    return results


def render_doctor_output(results: list[HealthCheck]) -> str:
    """Render health check results."""
    lines = ["=== CLI Health Check ===", ""]
    all_ok = True

    for result in results:
        status_icon = "✓" if result.status == "OK" else "✗"
        lines.append(f"{status_icon} {result.name}: {result.message}")
        if result.status != "OK":
            all_ok = False

    lines.append("")
    if all_ok:
        lines.append("All checks passed!")
    else:
        lines.append("Some checks failed. Please review the issues above.")

    return "\n".join(lines)
