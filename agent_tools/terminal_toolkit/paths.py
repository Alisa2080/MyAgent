"""Path helpers for the standalone terminal toolkit."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path


def _toolkit_home_candidates() -> Iterator[Path]:
    """Yield toolkit-home candidates in priority order."""
    custom = os.getenv("TERMINAL_TOOLKIT_HOME")
    if custom:
        yield Path(os.path.expanduser(custom))

    yield Path.home() / ".terminal-toolkit"
    yield Path.cwd() / ".terminal-toolkit"
    yield Path("/tmp") / ".terminal-toolkit"


def get_toolkit_home() -> Path:
    """Return the toolkit home directory used for snapshots and checkpoints."""
    for home in _toolkit_home_candidates():
        try:
            home.mkdir(parents=True, exist_ok=True)
            return home
        except OSError:
            continue
    raise OSError("Unable to create a writable toolkit home directory")


def display_toolkit_home() -> str:
    """Return a user-facing path string for the toolkit home directory."""
    try:
        return str(get_toolkit_home()).replace(str(Path.home()), "~", 1)
    except Exception:
        return str(get_toolkit_home())


def get_subprocess_home() -> str:
    """Return an isolated HOME override for subprocesses, or empty string."""
    custom = os.getenv("TERMINAL_TOOLKIT_SUBPROCESS_HOME")
    if custom:
        home = Path(os.path.expanduser(custom))
    elif os.getenv("TERMINAL_TOOLKIT_ISOLATE_HOME", "").lower() in ("1", "true", "yes", "on"):
        home = get_toolkit_home() / "home"
    else:
        return ""
    home.mkdir(parents=True, exist_ok=True)
    return str(home)
