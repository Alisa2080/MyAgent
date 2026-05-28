from __future__ import annotations

import logging
import os
from typing import Any

from cron.contracts import JobRunResult

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Supported modes
# ----------------------------------------------------------------------
_SUPPORTED_MODES = frozenset({"inprocess", "subprocess"})
_DEFAULT_MODE = "inprocess"


def runner_mode() -> str:
    """Return the configured runner mode, normalizing aliases."""
    raw = os.getenv("AGENT_CRON_RUNNER_MODE", "").strip().lower()
    if not raw:
        return _DEFAULT_MODE
    # Alias "process" → "subprocess" for discoverability
    if raw == "process":
        return "subprocess"
    return raw


def runner_mode_is_valid() -> bool:
    """Return True when the configured runner mode is supported."""
    return runner_mode() in _SUPPORTED_MODES


def runner_mode_diagnostic() -> tuple[str, bool]:
    """Return (mode_str, is_valid) for status/doctor output."""
    mode = runner_mode()
    return mode, mode in _SUPPORTED_MODES


def _failure_result(message: str) -> JobRunResult:
    return JobRunResult(
        success=False,
        output_doc=None,
        final_response=None,
        error=message,
    )


def run_job(job: dict[str, Any]) -> JobRunResult:
    """Execute a cron job, delegating to in-process or subprocess runner.

    The scheduler always calls this entrypoint.  Mode selection is fully
    internal so callers do not need to know how execution is performed.
    """
    mode = runner_mode()

    if mode not in _SUPPORTED_MODES:
        logger.error(
            "Unsupported AGENT_CRON_RUNNER_MODE=%r; falling back to failure result. "
            "Supported values: %s",
            mode,
            ", ".join(sorted(_SUPPORTED_MODES)),
        )
        return _failure_result(
            f"Unsupported runner mode: {mode!r}. "
            f"Set AGENT_CRON_RUNNER_MODE to one of: {', '.join(sorted(_SUPPORTED_MODES))}"
        )

    if mode == "inprocess":
        from cron.runner import run_job as run_job_inprocess

        return run_job_inprocess(job)

    # subprocess
    from cron.runner_subprocess import run_job_subprocess

    return run_job_subprocess(job)
