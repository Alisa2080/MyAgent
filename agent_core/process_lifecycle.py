from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
import time
from types import FrameType
from typing import Any

from agent_core.terminal_lifecycle import interrupt_all_terminal_waits
from agent_tools.terminal_toolkit.process_registry import process_registry
from agent_tools.terminal_toolkit.terminal_tool import cleanup_all_environments

logger = logging.getLogger(__name__)

SIGTERM_GRACE_ENV = "TERMINAL_SIGTERM_GRACE"
DEFAULT_SIGTERM_GRACE_SECONDS = 1.5

_install_lock = threading.Lock()
_installed = False
_atexit_registered = False
_cleanup_lock = threading.Lock()
_cleanup_done = False
_shutdown_requested = False
_previous_signal_handlers: dict[int, Any] = {}


def signal_grace_seconds() -> float:
    raw = os.getenv(SIGTERM_GRACE_ENV)
    if raw is None:
        return DEFAULT_SIGTERM_GRACE_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_SIGTERM_GRACE_SECONDS


def is_process_shutdown_requested() -> bool:
    """Return whether process shutdown has been requested by a signal handler."""
    return _shutdown_requested


def _signal_handler(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    try:
        interrupt_all_terminal_waits(reason=f"received_signal_{signum}")
        grace = signal_grace_seconds()
        if grace > 0:
            time.sleep(grace)
    except Exception:
        pass
    _call_previous_signal_handler(signum, frame)
    raise KeyboardInterrupt()


def _call_previous_signal_handler(signum: int, frame: FrameType | None) -> None:
    previous_handler = _previous_signal_handlers.get(signum)
    if previous_handler in (signal.SIG_DFL, signal.SIG_IGN, None, _signal_handler):
        return
    if not callable(previous_handler):
        return

    try:
        previous_handler(signum, frame)
    except Exception:
        pass


def run_process_shutdown_cleanup(*, reason: str = "process_exit") -> dict[str, Any]:
    global _cleanup_done
    with _cleanup_lock:
        if _cleanup_done:
            return {"cleaned": False, "reason": reason, "already_done": True}
        _cleanup_done = True

    killed_processes = 0
    cleaned_environments = 0
    errors: list[str] = []

    try:
        killed_processes = process_registry.kill_all()
    except Exception as exc:
        logger.exception("Failed to kill terminal toolkit background processes during shutdown.")
        errors.append(f"process_registry.kill_all: {exc}")

    try:
        cleaned_environments = cleanup_all_environments()
    except Exception as exc:
        logger.exception("Failed to clean terminal toolkit environments during shutdown.")
        errors.append(f"cleanup_all_environments: {exc}")

    return {
        "cleaned": True,
        "reason": reason,
        "killed_processes": killed_processes,
        "cleaned_environments": cleaned_environments,
        "errors": errors,
    }


def _atexit_cleanup() -> None:
    run_process_shutdown_cleanup(reason="atexit")


def _register_atexit_cleanup_once() -> None:
    global _atexit_registered
    if _atexit_registered:
        return

    try:
        atexit.unregister(cleanup_all_environments)
    except (AttributeError, ValueError):
        pass
    atexit.register(_atexit_cleanup)
    _atexit_registered = True


def install_process_signal_handlers() -> bool:
    global _installed
    with _install_lock:
        _register_atexit_cleanup_once()

        if _installed:
            return False

        if threading.current_thread() is not threading.main_thread():
            return False

        _previous_signal_handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, _signal_handler)
        if hasattr(signal, "SIGHUP"):
            _previous_signal_handlers[signal.SIGHUP] = signal.signal(signal.SIGHUP, _signal_handler)
        _installed = True
        return True
