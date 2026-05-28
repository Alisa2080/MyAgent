from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from agent_tools.terminal_toolkit.paths import get_toolkit_home


def get_cron_home() -> Path:
    cron_home = os.getenv("AGENT_CRON_HOME")
    if cron_home:
        return Path(os.path.expanduser(cron_home)).resolve()
    return get_toolkit_home().resolve()


def display_cron_home() -> str:
    home = get_cron_home()
    try:
        return str(home).replace(str(Path.home()), "~", 1)
    except Exception:
        return str(home)


def get_cron_dir() -> Path:
    return get_cron_home() / "cron"


def get_jobs_file() -> Path:
    return get_cron_dir() / "jobs.json"


def get_output_dir() -> Path:
    return get_cron_dir() / "output"


def get_scripts_dir() -> Path:
    return get_cron_home() / "scripts"


RUNNER_TMP_DIR_NAME = "runner-tmp"


def get_runner_tmp_dir() -> Path:
    return get_cron_dir() / RUNNER_TMP_DIR_NAME


def secure_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def secure_file(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def ensure_cron_dirs() -> None:
    for path in (get_cron_dir(), get_output_dir(), get_scripts_dir(), get_runner_tmp_dir()):
        path.mkdir(parents=True, exist_ok=True)
        secure_dir(path)


def atomic_replace(src: str | Path, dst: str | Path) -> None:
    os.replace(str(src), str(dst))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_cron_dirs()
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=f".{path.name}_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
