import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_core.workspace import WORKDIR

MAX_TOOL_OUTPUT_CHARS = 50000
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".backups",
    ".trash",
}


def truncate(text: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... (truncated, {len(text) - max_chars} more chars)"


def relative_path(path: Path) -> str:
    return path.relative_to(WORKDIR).as_posix()


def format_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def path_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    info = {
        "path": relative_path(path),
        "type": "directory" if path.is_dir() else "file",
        "size_bytes": stat.st_size,
        "modified_utc": format_mtime(stat.st_mtime),
        "created_utc": format_mtime(stat.st_ctime),
    }
    if path.is_dir():
        info["entries"] = sum(1 for _ in path.iterdir())
    return info
