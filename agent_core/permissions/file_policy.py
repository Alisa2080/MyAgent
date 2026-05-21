from __future__ import annotations

import os
import posixpath
from pathlib import Path

from agent_core.permissions.models import PolicyDecision
from agent_core.workspace import WORKDIR
from agent_tools.file_toolkit.backend_paths import (
    allowed_workspace_roots_for_task,
    path_is_under_any_root,
    resolve_path_for_policy,
)
from agent_core.permissions.constants import UNRESOLVED_BACKEND_WRITE_PATH


SENSITIVE_HOST_NAMES = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}
SENSITIVE_HOST_FILES = {".netrc", ".npmrc", ".pypirc", ".pgpass"}
SENSITIVE_HOST_SUBPATHS = {(".config", "gh")}
SENSITIVE_CONTAINER_PREFIXES = (
    "/root/.ssh/",
    "/root/.aws/",
    "/root/.gnupg/",
    "/root/.kube/",
    "/root/.docker/",
    "/root/.azure/",
    "/root/.config/gh/",
    "/etc/",
    "/boot/",
    "/usr/lib/systemd/",
    "/private/etc/",
    "/private/var/",
)
SENSITIVE_EXACT = {"/etc", "/var/run/docker.sock", "/run/docker.sock"}


def is_sensitive_path(path: str) -> bool:
    normalized_raw = _normalize_posix_path(str(path))
    if _is_sensitive_container_path(normalized_raw):
        return True

    try:
        expanded_path = Path(_expand_user(path))
        home_lexical = Path.home().expanduser()
        if _is_sensitive_home_relative(expanded_path, home_lexical):
            return True
        if not expanded_path.is_absolute():
            return False
        home = Path.home().expanduser()
    except (OSError, RuntimeError):
        return False

    if _is_sensitive_home_relative(expanded_path, home):
        return True
    return False


def _is_sensitive_home_relative(path: Path, home: Path) -> bool:
    try:
        relative = path.relative_to(home)
    except ValueError:
        return False

    parts = relative.parts
    if not parts:
        return False
    if parts[0] in SENSITIVE_HOST_NAMES:
        return True
    for subpath in SENSITIVE_HOST_SUBPATHS:
        if parts[: len(subpath)] == subpath:
            return True
    return len(parts) == 1 and parts[0] in SENSITIVE_HOST_FILES


def classify_file_write(path: str, *, task_id: str, resolved_path: str | None = None) -> PolicyDecision:
    if is_sensitive_path(path):
        return PolicyDecision.deny(
            "sensitive_path",
            risk_tags=("sensitive_path",),
            message=f"Write denied for sensitive path: {path}",
            data={"path": str(path)},
        )

    try:
        resolved_path = str(resolved_path or resolve_path_for_policy(path, task_id))
    except Exception as exc:
        return PolicyDecision.review(
            "path_resolution_failed",
            risk_tags=("path_resolution_failed",),
            data={"path": str(path), "error": str(exc)},
        )
    if resolved_path == UNRESOLVED_BACKEND_WRITE_PATH:
        return PolicyDecision.deny(
            "path_resolution_failed",
            risk_tags=("path_resolution_failed",),
            message=f"Write denied because backend path resolution failed: {path}",
            data={"path": str(path), "resolved_path": resolved_path},
        )
    if is_sensitive_path(resolved_path):
        return PolicyDecision.deny(
            "sensitive_path",
            risk_tags=("sensitive_path",),
            message=f"Write denied for sensitive path: {path}",
            data={"path": str(path), "resolved_path": resolved_path},
        )

    roots = _allowed_workspace_roots(task_id)
    if path_is_under_any_root(resolved_path, roots):
        return PolicyDecision.allow("workspace_write", data={"path": resolved_path})

    return PolicyDecision.review(
        "writes_outside_workspace",
        risk_tags=("writes_outside_workspace",),
        message=f"Write requires approval because it targets outside the workspace: {path}",
        data={"path": resolved_path},
    )


def approved_write_root_for_path(path: str, *, task_id: str, resolved_path: str | None = None) -> str:
    resolved_path = str(resolved_path or resolve_path_for_policy(path, task_id))
    parent = posixpath.dirname(_normalize_posix_path(resolved_path))
    return parent or "/"


def _allowed_workspace_roots(task_id: str) -> list[str]:
    try:
        roots = [root for root in allowed_workspace_roots_for_task(task_id) if root]
    except Exception:
        roots = []
    if roots:
        return roots

    try:
        workdir_root = str(Path(WORKDIR).expanduser().resolve())
    except (OSError, RuntimeError):
        return []
    return [workdir_root]


def _expand_user(path: str) -> str:
    path_str = os.path.expandvars(os.fspath(path))
    if path_str == "~":
        return str(Path.home())
    if path_str.startswith("~/"):
        return str(Path.home() / path_str[2:])
    return os.path.expanduser(path_str)


def _is_sensitive_container_path(path: str) -> bool:
    if path in SENSITIVE_EXACT:
        return True
    for prefix in SENSITIVE_CONTAINER_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def _normalize_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized
