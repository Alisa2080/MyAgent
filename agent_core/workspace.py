import os
from pathlib import Path


def resolve_workdir() -> Path:
    configured = os.getenv("CODE_AGENT_WORKDIR") or os.getenv("WORKDIR")
    path = Path(configured).expanduser() if configured else Path.cwd()
    resolved = path.resolve()
    if not resolved.exists():
        raise RuntimeError(f"Workspace directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise RuntimeError(f"Workspace path is not a directory: {resolved}")
    return resolved


WORKDIR = resolve_workdir()


def safe_path(path_str: str) -> Path:
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path
