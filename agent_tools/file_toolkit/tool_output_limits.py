"""Configurable output limits for file toolkit output."""

import os

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_LINE_LENGTH = 2000


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def get_max_lines() -> int:
    return _positive_int_env("AGENT_FILE_MAX_LINES", DEFAULT_MAX_LINES)


def get_max_line_length() -> int:
    return _positive_int_env("AGENT_FILE_MAX_LINE_LENGTH", DEFAULT_MAX_LINE_LENGTH)
