"""Generic file safety rules for low-level file operations."""

from __future__ import annotations

import os
from typing import Iterable, Optional


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved AGENT_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("AGENT_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def _is_under_root(path: str, root: str) -> bool:
    """Return True if *path* is equal to or contained by *root*."""
    return path == root or path.startswith(root + os.sep)


def is_write_denied(
    path: str,
    extra_allowed_roots: Optional[Iterable[str]] = None,
    *,
    safe_root_applies: bool = True,
) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    allowed_roots = []
    for root in extra_allowed_roots or []:
        try:
            allowed_roots.append(os.path.realpath(os.path.expanduser(str(root))))
        except Exception:
            continue

    safe_root = get_safe_write_root() if safe_root_applies else None
    if safe_root and _is_under_root(resolved, safe_root):
        return False

    if allowed_roots:
        for allowed_root in allowed_roots:
            if _is_under_root(resolved, allowed_root):
                return False
        if not safe_root_applies:
            return True

    if safe_root:
        return True

    return False
