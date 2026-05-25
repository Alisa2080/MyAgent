from __future__ import annotations

import os
from pathlib import Path


DEFAULT_HOME_NAME = ".langchain-agent"
DB_FILENAME = "cli.sqlite"


def get_cli_home() -> Path:
    configured = os.getenv("AGENT_CLI_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / DEFAULT_HOME_NAME


def ensure_cli_home() -> Path:
    home = get_cli_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_db_path() -> Path:
    return get_cli_home() / DB_FILENAME


def ensure_db_parent() -> Path:
    home = ensure_cli_home()
    return home / DB_FILENAME
