from __future__ import annotations

import re

from agent_tools.file_toolkit.redact import redact_sensitive_text


_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{10,}|"
    r"github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[A-Z0-9]{16}|"
    r"AIza[A-Za-z0-9_-]{30,}"
    r")(?![A-Za-z0-9_-])"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def redact_code_execution_text(text: str) -> str:
    cleaned = redact_sensitive_text(str(text or ""))
    cleaned = _SECRET_RE.sub("[REDACTED]", cleaned)
    return _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", cleaned)


def redact_code_execution_value(value):
    if isinstance(value, str):
        return redact_code_execution_text(value)
    if isinstance(value, dict):
        return {
            key: redact_code_execution_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_code_execution_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_code_execution_value(item) for item in value)
    return value
