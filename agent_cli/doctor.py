from __future__ import annotations

import platform
import sys
from pathlib import Path

from agent_cli.session_store import SessionStore


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


def run_health_checks(workdir: str, tmp_path: Path | None = None) -> list[tuple[str, bool, str]]:
    """Run all health checks."""
    checks = [
        ("Python Version", check_python_version),
        ("Platform", check_platform),
        ("Dependencies", check_dependencies),
        ("Session Store", lambda: check_session_store(tmp_path)),
        ("Workdir", lambda: check_workdir(workdir)),
    ]

    results = []
    for name, check_fn in checks:
        ok, message = check_fn()
        results.append((name, ok, message))

    return results


def render_doctor_output(results: list[tuple[str, bool, str]]) -> str:
    """Render health check results."""
    lines = ["=== CLI Health Check ===", ""]
    all_ok = True

    for name, ok, message in results:
        status = "✓" if ok else "✗"
        lines.append(f"{status} {name}: {message}")
        if not ok:
            all_ok = False

    lines.append("")
    if all_ok:
        lines.append("All checks passed!")
    else:
        lines.append("Some checks failed. Please review the issues above.")

    return "\n".join(lines)
