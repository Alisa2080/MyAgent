"""Path helpers for the standalone Hermes terminal toolkit."""

from __future__ import annotations

import os
from pathlib import Path


def get_toolkit_home() -> Path:
    """Return the toolkit home directory used for snapshots and checkpoints."""
    custom = os.getenv("HERMES_TERMINAL_TOOLKIT_HOME")
    if custom:
        candidates = [Path(os.path.expanduser(custom))]
    else:
        hermes_home = os.getenv("HERMES_HOME")
        if hermes_home:
            candidates = [Path(os.path.expanduser(hermes_home)) / "terminal-toolkit"]
        else:
            candidates = [
                Path.home() / ".hermes-terminal-toolkit",
                Path.cwd() / ".hermes-terminal-toolkit",
                Path("/tmp") / ".hermes-terminal-toolkit",
            ]
    for home in candidates:
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
    custom = os.getenv("HERMES_TERMINAL_TOOLKIT_SUBPROCESS_HOME")
    if custom:
        home = Path(os.path.expanduser(custom))
    elif os.getenv("HERMES_TERMINAL_TOOLKIT_ISOLATE_HOME", "").lower() in ("1", "true", "yes", "on"):
        home = get_toolkit_home() / "home"
    else:
        return ""
    home.mkdir(parents=True, exist_ok=True)
    return str(home)
