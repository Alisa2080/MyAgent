from __future__ import annotations


CHILD_POLICY_SITE_CUSTOMIZE = r'''
from __future__ import annotations

import builtins
import os
import socket
import sys
import sysconfig

_SANDBOX_ROOT = os.path.realpath(os.environ.get("CODE_EXECUTION_SANDBOX_ROOT", ""))
_RPC_SOCKET = os.environ.get("CODE_EXECUTION_RPC_SOCKET", "")


def _roots_from_env(name):
    roots = []
    for raw in os.environ.get(name, "").split(os.pathsep):
        if raw:
            roots.append(os.path.realpath(raw))
    return roots


_READ_ROOTS = _roots_from_env("CODE_EXECUTION_ALLOWED_READ_ROOTS")
for _path in (
    sys.prefix,
    getattr(sys, "base_prefix", ""),
    sysconfig.get_path("stdlib") or "",
    sysconfig.get_path("platstdlib") or "",
    sysconfig.get_path("purelib") or "",
    sysconfig.get_path("platlib") or "",
):
    if _path:
        _READ_ROOTS.append(os.path.realpath(_path))


def _under(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _deny(action):
    raise PermissionError(
        f"execute_code policy denied {action}; use hermes_tools RPC wrappers for project files, terminal, and web access."
    )


def _is_write_open(mode, flags):
    mode_text = str(mode or "")
    if any(m in mode_text for m in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return False


def _guard_open(file, mode="r", flags=0):
    if isinstance(file, int):
        return
    if not isinstance(file, (str, bytes, os.PathLike)):
        return
    path_text = os.fsdecode(file)
    if not path_text:
        return
    if not os.path.isabs(path_text):
        path_text = os.path.join(os.getcwd(), path_text)
    if _is_write_open(mode, flags):
        if _SANDBOX_ROOT and _under(path_text, (_SANDBOX_ROOT,)):
            return
        _deny(f"direct file write to {path_text}")
    if _under(path_text, _READ_ROOTS):
        return
    _deny(f"direct file read from {path_text}")


_original_open = builtins.open


def _policy_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    _guard_open(file, mode=mode)
    return _original_open(file, mode, buffering, encoding, errors, newline, closefd, opener)


builtins.open = _policy_open


def _audit(event, args):
    if event == "open":
        path, mode, flags = (args + (None, None, 0))[:3]
        _guard_open(path, mode=mode, flags=flags)
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "pty.spawn"}:
        _deny(event)
    elif event == "socket.connect":
        sock, address = args
        if getattr(sock, "family", None) == socket.AF_UNIX and address == _RPC_SOCKET:
            return
        _deny("direct socket connect")


sys.addaudithook(_audit)
'''
