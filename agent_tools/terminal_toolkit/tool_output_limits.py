"""Output truncation limits for the standalone terminal toolkit."""

from __future__ import annotations


DEFAULT_MAX_BYTES = 50_000


def get_max_bytes() -> int:
    """Return the terminal output cap, controlled by env var when set."""
    import os

    raw = os.getenv("TERMINAL_TOOLKIT_MAX_BYTES")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES
