from __future__ import annotations

import re


WORKDIR_SAFE_RE = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +@=,]+$")
SHELL_LEVEL_BACKGROUND_RE = re.compile(r"\b(?:nohup|disown|setsid)\b", re.IGNORECASE)
INLINE_BACKGROUND_AMP_RE = re.compile(r"\s&\s")
TRAILING_BACKGROUND_AMP_RE = re.compile(r"\s&\s*(?:#.*)?$")
LONG_LIVED_FOREGROUND_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+compose\s+up\b", re.IGNORECASE),
    re.compile(r"\bnext\s+dev\b", re.IGNORECASE),
    re.compile(r"\bvite(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bnodemon\b", re.IGNORECASE),
    re.compile(r"\buvicorn\b", re.IGNORECASE),
    re.compile(r"\bgunicorn\b", re.IGNORECASE),
    re.compile(r"\bpython(?:3)?\s+-m\s+http\.server\b", re.IGNORECASE),
)


def _validate_workdir(workdir: str) -> str | None:
    if not workdir:
        return None
    if not WORKDIR_SAFE_RE.match(workdir):
        for ch in workdir:
            if not WORKDIR_SAFE_RE.match(ch):
                return (
                    f"Blocked: workdir contains disallowed character {repr(ch)}. "
                    "Use a simple filesystem path without shell metacharacters."
                )
        return "Blocked: workdir contains disallowed characters."
    return None


def _handle_sudo_failure(output: str) -> str:
    sudo_failures = [
        "sudo: a password is required",
        "sudo: no tty present",
        "sudo: a terminal is required",
    ]
    for failure in sudo_failures:
        if failure in output:
            return output + "\n\nTip: set SUDO_PASSWORD in the environment to enable non-interactive sudo."
    return output


def _looks_like_help_or_version_command(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    return " --help" in normalized or normalized.endswith(" -h") or " --version" in normalized or normalized.endswith(" -v")


def _foreground_background_guidance(command: str) -> str | None:
    if _looks_like_help_or_version_command(command):
        return None
    if SHELL_LEVEL_BACKGROUND_RE.search(command):
        return (
            "Foreground command uses shell-level background wrappers (nohup/disown/setsid). "
            "Use background=true so the toolkit can track the process."
        )
    if INLINE_BACKGROUND_AMP_RE.search(command) or TRAILING_BACKGROUND_AMP_RE.search(command):
        return (
            "Foreground command uses '&' backgrounding. Use background=true for long-lived "
            "processes, then run health checks and tests in follow-up terminal calls."
        )
    for pattern in LONG_LIVED_FOREGROUND_PATTERNS:
        if pattern.search(command):
            return (
                "This foreground command appears to start a long-lived server/watch process. "
                "Run it with background=true, verify readiness, then execute tests separately."
            )
    return None


def _resolve_notification_flag_conflict(*, notify_on_complete: bool, watch_patterns, background: bool) -> tuple:
    if background and notify_on_complete and watch_patterns:
        return None, (
            "watch_patterns ignored because notify_on_complete=True; "
            "these two flags produce duplicate notifications when combined"
        )
    return watch_patterns, ""


def _command_requires_pipe_stdin(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    return normalized.startswith("gh auth login") and "--with-token" in normalized


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    if exit_code == 0:
        return None
    segments = re.split(r"\s*(?:\|\||&&|[|;])\s*", command)
    last_segment = (segments[-1] if segments else command).strip()
    words = last_segment.split()
    base_cmd = ""
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue
        base_cmd = w.split("/")[-1]
        break
    semantics: dict[str, dict[int, str]] = {
        "grep": {1: "No matches found (not an error)"},
        "egrep": {1: "No matches found (not an error)"},
        "fgrep": {1: "No matches found (not an error)"},
        "rg": {1: "No matches found (not an error)"},
        "ag": {1: "No matches found (not an error)"},
        "ack": {1: "No matches found (not an error)"},
        "diff": {1: "Files differ (expected, not an error)"},
        "colordiff": {1: "Files differ (expected, not an error)"},
        "find": {1: "Some directories were inaccessible (partial results may still be valid)"},
        "test": {1: "Condition evaluated to false (expected, not an error)"},
        "[": {1: "Condition evaluated to false (expected, not an error)"},
        "curl": {
            6: "Could not resolve host",
            7: "Failed to connect to host",
            22: "HTTP response code indicated error (e.g. 404, 500)",
            28: "Operation timed out",
        },
        "git": {1: "Non-zero exit (often normal — e.g. 'git diff' returns 1 when files differ)"},
    }
    return semantics.get(base_cmd, {}).get(exit_code)
