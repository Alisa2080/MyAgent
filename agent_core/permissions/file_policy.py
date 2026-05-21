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


SENSITIVE_HOST_NAMES = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}
SENSITIVE_HOST_FILES = {".netrc", ".npmrc", ".pypirc", ".pgpass"}
SENSITIVE_CONTAINER_PREFIXES = (
    "/root/.ssh/",
    "/root/.aws/",
    "/root/.gnupg/",
    "/root/.kube/",
    "/root/.docker/",
    "/root/.azure/",
    "/etc/",
)
SENSITIVE_EXACT = {"/etc", "/var/run/docker.sock", "/run/docker.sock"}


def is_sensitive_path(path: str) -> bool:
    normalized_raw = _normalize_posix_path(str(path))
    if _is_sensitive_container_path(normalized_raw):
        return True

    try:
        host_path = _host_path(path)
        home = Path.home().expanduser().resolve()
    except (OSError, RuntimeError):
        return False

    try:
        relative = host_path.relative_to(home)
    except ValueError:
        return False

    parts = relative.parts
    if not parts:
        return False
    if parts[0] in SENSITIVE_HOST_NAMES:
        return True
    return len(parts) == 1 and parts[0] in SENSITIVE_HOST_FILES


def classify_file_write(path: str, *, task_id: str) -> PolicyDecision:
    if is_sensitive_path(path):
        return PolicyDecision.deny(
            "sensitive_path",
            risk_tags=("sensitive_path",),
            data={"path": str(path)},
        )

    try:
        resolved_path = resolve_path_for_policy(path, task_id)
    except Exception as exc:
        return PolicyDecision.review(
            "path_resolution_failed",
            risk_tags=("path_resolution_failed",),
            data={"path": str(path), "error": str(exc)},
        )

    roots = _allowed_workspace_roots(task_id)
    if path_is_under_any_root(resolved_path, roots):
        return PolicyDecision.allow("workspace_write", data={"path": resolved_path})

    return PolicyDecision.review(
        "writes_outside_workspace",
        risk_tags=("writes_outside_workspace",),
        data={"path": resolved_path},
    )


def approved_write_root_for_path(path: str, *, task_id: str) -> str:
    resolved_path = resolve_path_for_policy(path, task_id)
    parent = posixpath.dirname(_normalize_posix_path(resolved_path))
    return parent or "/"


def _allowed_workspace_roots(task_id: str) -> list[str]:
    roots = list(allowed_workspace_roots_for_task(task_id))
    try:
        workdir_root = str(Path(WORKDIR).expanduser().resolve())
    except (OSError, RuntimeError):
        return roots
    if workdir_root not in roots:
        roots.append(workdir_root)
    return roots


def _host_path(path: str) -> Path:
    return Path(_expand_user(path)).resolve()


def _expand_user(path: str) -> str:
    path_str = os.fspath(path)
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
