"""Shared terminal command helpers extracted from Hermes terminal_tool."""

from __future__ import annotations

import logging
import os
import platform
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_sudo_password_cache: dict[str, str] = {}
_sudo_password_cache_lock = threading.Lock()
_callback_tls = threading.local()


def _get_sudo_password_callback():
    return getattr(_callback_tls, "sudo_password", None)


def set_sudo_password_callback(cb):
    _callback_tls.sudo_password = cb


def _get_sudo_password_cache_scope() -> str:
    callback = _get_sudo_password_callback()
    if callback is not None:
        owner = getattr(callback, "__self__", None)
        func = getattr(callback, "__func__", None)
        if owner is not None and func is not None:
            return f"callback-owner:{id(owner)}:{id(func)}"
        return f"callback:{id(callback)}"
    return f"thread:{threading.get_ident()}"


def _get_cached_sudo_password() -> str:
    scope = _get_sudo_password_cache_scope()
    with _sudo_password_cache_lock:
        return _sudo_password_cache.get(scope, "")


def _set_cached_sudo_password(password: str) -> None:
    scope = _get_sudo_password_cache_scope()
    with _sudo_password_cache_lock:
        if password:
            _sudo_password_cache[scope] = password
        else:
            _sudo_password_cache.pop(scope, None)


def _prompt_for_sudo_password(timeout_seconds: int = 45) -> str:
    import sys

    sudo_cb = _get_sudo_password_callback()
    if sudo_cb is not None:
        try:
            return sudo_cb() or ""
        except Exception:
            return ""

    result = {"password": None, "done": False}

    def read_password_thread():
        tty_fd = None
        old_attrs = None
        try:
            if platform.system() == "Windows":
                import msvcrt

                chars = []
                while True:
                    c = msvcrt.getwch()
                    if c in ("\r", "\n"):
                        break
                    if c == "\x03":
                        raise KeyboardInterrupt
                    chars.append(c)
                result["password"] = "".join(chars)
            else:
                import termios

                tty_fd = os.open("/dev/tty", os.O_RDONLY)
                old_attrs = termios.tcgetattr(tty_fd)
                new_attrs = termios.tcgetattr(tty_fd)
                new_attrs[3] = new_attrs[3] & ~termios.ECHO
                termios.tcsetattr(tty_fd, termios.TCSAFLUSH, new_attrs)
                chars = []
                while True:
                    b = os.read(tty_fd, 1)
                    if not b or b in (b"\n", b"\r"):
                        break
                    chars.append(b)
                result["password"] = b"".join(chars).decode("utf-8", errors="replace")
        except Exception:
            result["password"] = ""
        finally:
            if tty_fd is not None and old_attrs is not None:
                try:
                    import termios as _termios

                    _termios.tcsetattr(tty_fd, _termios.TCSAFLUSH, old_attrs)
                except Exception:
                    pass
            if tty_fd is not None:
                try:
                    os.close(tty_fd)
                except Exception:
                    pass
            result["done"] = True

    try:
        os.environ["HERMES_SPINNER_PAUSE"] = "1"
        time.sleep(0.2)
        print()
        print("  Password (hidden): ", end="", flush=True)
        password_thread = threading.Thread(target=read_password_thread, daemon=True)
        password_thread.start()
        password_thread.join(timeout=timeout_seconds)
        if result["done"]:
            password = result["password"] or ""
            print()
            sys.stdout.flush()
            return password
        print()
        return ""
    finally:
        os.environ.pop("HERMES_SPINNER_PAUSE", None)


def _read_shell_token(command: str, start: int) -> tuple[str, int]:
    i = start
    n = len(command)
    while i < n:
        ch = command[i]
        if ch.isspace() or ch in ";|&()":
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                inner = command[i]
                if inner == "\\" and i + 1 < n:
                    i += 2
                    continue
                if inner == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return command[start:i], i


def _rewrite_real_sudo_invocations(command: str) -> tuple[str, bool]:
    out: list[str] = []
    i = 0
    n = len(command)
    command_start = True
    found = False
    while i < n:
        ch = command[i]
        if ch.isspace():
            out.append(ch)
            if ch == "\n":
                command_start = True
            i += 1
            continue
        if ch == "#" and command_start:
            comment_end = command.find("\n", i)
            if comment_end == -1:
                out.append(command[i:])
                break
            out.append(command[i:comment_end])
            i = comment_end
            continue
        if ch in ";|&(":
            out.append(ch)
            command_start = True
            i += 1
            continue
        token, next_i = _read_shell_token(command, i)
        if command_start and token == "sudo":
            out.append("sudo -S -p ''")
            found = True
        else:
            out.append(token)
        command_start = False
        i = next_i
    return "".join(out), found


def rewrite_compound_background(command: str) -> str:
    """Rewrite `A && B &` into `A && { B & }` to avoid bash subshell waits."""
    if "&" not in command:
        return command
    lines = command.splitlines()
    rewritten = []
    pattern = re.compile(r"^(?P<prefix>.*(?:&&|\|\||;))\s*(?P<tail>[^#].*?)\s*&\s*(?P<comment>#.*)?$")
    for line in lines:
        match = pattern.match(line)
        if match:
            prefix = match.group("prefix").rstrip()
            tail = match.group("tail").strip()
            comment = match.group("comment") or ""
            rewritten.append(f"{prefix} {{ {tail} & }}{(' ' + comment) if comment else ''}")
        else:
            rewritten.append(line)
    return "\n".join(rewritten)


def transform_sudo_command(command: str | None) -> tuple[str | None, str | None]:
    if not command:
        return command, None
    transformed, has_real_sudo = _rewrite_real_sudo_invocations(command)
    if not has_real_sudo:
        return command, None
    has_configured_password = bool(os.getenv("SUDO_PASSWORD"))
    sudo_password = os.getenv("SUDO_PASSWORD", "") if has_configured_password else _get_cached_sudo_password()
    if not has_configured_password and not sudo_password and os.getenv("HERMES_INTERACTIVE"):
        sudo_password = _prompt_for_sudo_password(timeout_seconds=45)
        if sudo_password:
            _set_cached_sudo_password(sudo_password)
    if has_configured_password or sudo_password:
        return transformed, sudo_password + "\n"
    return transformed, None


def safe_command_preview(command: Any, limit: int = 200) -> str:
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"
