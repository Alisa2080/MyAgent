from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STDOUT_LIMIT_CHARS = 50_000
DEFAULT_STDERR_LIMIT_CHARS = 10_000
DEFAULT_OUTPUT_LIMIT_CHARS = 100_000
MAX_TIMEOUT_SECONDS = 3_600
MAX_TOOL_CALLS = 500
MAX_OUTPUT_LIMIT_CHARS = 1_000_000
DEFAULT_SAFE_ENV_NAMES = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
})
DEFAULT_SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
DEFAULT_SECRET_DENYLIST = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PASSWD",
    "AUTH",
)

logger = logging.getLogger(__name__)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_positive_int(
    *,
    name: str,
    raw: object,
    default: int,
    maximum: int,
    warnings: list[str],
) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warnings.append(f"{name}={raw!r} is invalid; using {default}.")
        return default
    if value <= 0:
        warnings.append(f"{name}={raw!r} must be positive; using {default}.")
        return default
    if value > maximum:
        warnings.append(f"{name}={raw!r} exceeds maximum {maximum}; using {maximum}.")
        return maximum
    return value


@dataclass(frozen=True)
class CodeExecutionConfig:
    mode: str = "project"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    stdout_limit_chars: int = DEFAULT_STDOUT_LIMIT_CHARS
    stderr_limit_chars: int = DEFAULT_STDERR_LIMIT_CHARS
    output_limit_chars: int = DEFAULT_OUTPUT_LIMIT_CHARS
    env_allowlist: tuple[str, ...] = ()
    secret_denylist: tuple[str, ...] = DEFAULT_SECRET_DENYLIST
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_sources(cls, explicit: dict[str, object] | None = None) -> "CodeExecutionConfig":
        explicit = dict(explicit or {})
        warnings: list[str] = []

        raw_mode = explicit.get("mode", os.getenv("CODE_EXECUTION_MODE", "project"))
        mode = str(raw_mode or "project").strip().lower()
        if mode not in {"project", "strict"}:
            warnings.append(f"CODE_EXECUTION_MODE={raw_mode!r} is invalid; using 'project'.")
            mode = "project"

        timeout_seconds = _parse_positive_int(
            name="CODE_EXECUTION_TIMEOUT_SECONDS",
            raw=explicit.get("timeout_seconds", os.getenv("CODE_EXECUTION_TIMEOUT_SECONDS")),
            default=DEFAULT_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
            warnings=warnings,
        )
        max_tool_calls = _parse_positive_int(
            name="CODE_EXECUTION_MAX_TOOL_CALLS",
            raw=explicit.get("max_tool_calls", os.getenv("CODE_EXECUTION_MAX_TOOL_CALLS")),
            default=DEFAULT_MAX_TOOL_CALLS,
            maximum=MAX_TOOL_CALLS,
            warnings=warnings,
        )
        stdout_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_STDOUT_LIMIT_CHARS",
            raw=explicit.get("stdout_limit_chars", os.getenv("CODE_EXECUTION_STDOUT_LIMIT_CHARS")),
            default=DEFAULT_STDOUT_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )
        stderr_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_STDERR_LIMIT_CHARS",
            raw=explicit.get("stderr_limit_chars", os.getenv("CODE_EXECUTION_STDERR_LIMIT_CHARS")),
            default=DEFAULT_STDERR_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )
        output_limit_chars = _parse_positive_int(
            name="CODE_EXECUTION_OUTPUT_LIMIT_CHARS",
            raw=explicit.get("output_limit_chars", os.getenv("CODE_EXECUTION_OUTPUT_LIMIT_CHARS")),
            default=DEFAULT_OUTPUT_LIMIT_CHARS,
            maximum=MAX_OUTPUT_LIMIT_CHARS,
            warnings=warnings,
        )

        explicit_allowlist_raw = explicit.get("env_allowlist", ())
        explicit_allowlist = (
            tuple(str(x).strip() for x in explicit_allowlist_raw if str(x).strip())
            if isinstance(explicit_allowlist_raw, (list, tuple, set))
            else ()
        )
        env_allowlist = (
            tuple(dict.fromkeys(explicit_allowlist))
            if "env_allowlist" in explicit
            else _split_csv(os.getenv("CODE_EXECUTION_ENV_ALLOWLIST"))
        )

        explicit_denylist_raw = explicit.get("secret_denylist", ())
        explicit_denylist = (
            tuple(str(x).strip().upper() for x in explicit_denylist_raw if str(x).strip())
            if isinstance(explicit_denylist_raw, (list, tuple, set))
            else ()
        )
        secret_denylist = tuple(dict.fromkeys([
            *DEFAULT_SECRET_DENYLIST,
            *_split_csv(os.getenv("CODE_EXECUTION_SECRET_DENYLIST")),
            *explicit_denylist,
        ]))

        for warning in warnings:
            logger.warning("code_execution config fallback: %s", warning)

        return cls(
            mode=mode,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
            stdout_limit_chars=stdout_limit_chars,
            stderr_limit_chars=stderr_limit_chars,
            output_limit_chars=output_limit_chars,
            env_allowlist=env_allowlist,
            secret_denylist=secret_denylist,
            warnings=tuple(warnings),
        )

    def env_name_allowed(self, name: str) -> bool:
        upper = str(name or "").upper()
        if any(marker and marker.upper() in upper for marker in self.secret_denylist):
            return False
        if name in DEFAULT_SAFE_ENV_NAMES:
            return True
        if any(name.startswith(prefix) for prefix in DEFAULT_SAFE_ENV_PREFIXES):
            return True
        return name in self.env_allowlist
