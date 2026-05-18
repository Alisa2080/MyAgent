import os
import re

from agent_core.workspace import WORKDIR, safe_path


os.environ["TERMINAL_CWD"] = str(WORKDIR)
os.environ["AGENT_WRITE_SAFE_ROOT"] = str(WORKDIR)


def ensure_workspace_path(path: str) -> str | None:
    try:
        safe_path(path or ".")
    except Exception as exc:
        return str(exc)
    return None


def ensure_read_allowed(path: str) -> str | None:
    try:
        resolved = safe_path(path or ".")
    except Exception as exc:
        return str(exc)

    blocked_dirs = [
        WORKDIR / "skills" / ".hub",
        WORKDIR / "skills" / ".hub" / "index-cache",
    ]
    for blocked_dir in blocked_dirs:
        try:
            resolved.relative_to(blocked_dir.resolve())
        except ValueError:
            continue
        return (
            f"Access denied: {path} is an internal skill cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None


def ensure_patch_paths(patch_content: str | None) -> str | None:
    if not patch_content:
        return None
    for match in re.finditer(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+)$", patch_content, re.MULTILINE):
        path_error = ensure_workspace_path(match.group(1).strip())
        if path_error:
            return path_error
    for match in re.finditer(r"^\*\*\*\s+Move to:\s*(.+)$", patch_content, re.MULTILINE):
        path_error = ensure_workspace_path(match.group(1).strip())
        if path_error:
            return path_error
    return None
