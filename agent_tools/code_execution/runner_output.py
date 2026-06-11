from __future__ import annotations

import re

from agent_tools.code_execution.config import CodeExecutionConfig
from agent_tools.code_execution.safety import redact_code_execution_text


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def sanitize_output(text: str, *, limit: int) -> tuple[str, bool]:
    cleaned = ANSI_RE.sub("", str(text or ""))
    cleaned = redact_code_execution_text(cleaned)
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit] + "\n[truncated]", True


def _result_data(stdout: str, stderr: str, returncode: int, *, stdout_truncated: bool, stderr_truncated: bool) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def sanitize_process_output(
    *,
    stdout_raw: str,
    stderr_raw: str,
    returncode: int,
    config: CodeExecutionConfig,
) -> dict:
    """Apply per-stream sanitization then enforce a combined output limit.

    Secrets are redacted before the total-limit budget is computed so that
    redaction itself does not count toward the budget. Each stream is capped
    at its individual limit before the combined budget is applied.
    """
    stdout, stdout_truncated = sanitize_output(stdout_raw, limit=config.stdout_limit_chars)
    stderr, stderr_truncated = sanitize_output(stderr_raw, limit=config.stderr_limit_chars)
    combined_len = len(stdout) + len(stderr)
    if combined_len > config.output_limit_chars:
        stderr_reservation = min(len(stderr), config.output_limit_chars // 2)
        stdout_budget = min(
            len(stdout),
            max(0, config.output_limit_chars - stderr_reservation),
        )
        stderr_budget = max(0, config.output_limit_chars - stdout_budget)
        if len(stdout) > stdout_budget:
            stdout = stdout[:stdout_budget]
            stdout_truncated = True
        if len(stderr) > stderr_budget:
            stderr = stderr[:stderr_budget]
            stderr_truncated = True
    return _result_data(
        stdout,
        stderr,
        int(returncode),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
