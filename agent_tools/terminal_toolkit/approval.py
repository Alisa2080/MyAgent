"""Minimal dangerous-command guard for the standalone terminal toolkit."""

from __future__ import annotations

import re


_RE_FLAGS = re.IGNORECASE | re.DOTALL

_CMDPOS = (
    r"(?:^|[;&|\n`]|\$\()"
    r"\s*"
    r"(?:sudo\s+(?:-[^\s]+\s+)*)?"
    r"(?:env\s+(?:\w+=\S*\s+)*)?"
    r"(?:(?:exec|nohup|setsid|time)\s+)*"
    r"\s*"
)

HARDLINE_PATTERNS = [
    (r"\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)", "recursive delete of root filesystem"),
    (r"\brm\s+(-[^\s]*\s+)*(/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*)(\s|$)", "recursive delete of system directory"),
    (r"\brm\s+(-[^\s]*\s+)*(~|\$HOME)(/?|/\*)?(\s|$)", "recursive delete of home directory"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "format filesystem (mkfs)"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", "dd to raw block device"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "redirect to raw block device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bkill\s+(-[^\s]+\s+)*-1\b", "kill all processes"),
    (_CMDPOS + r"(shutdown|reboot|halt|poweroff)\b", "system shutdown/reboot"),
    (_CMDPOS + r"init\s+[06]\b", "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r"systemctl\s+(poweroff|reboot|halt|kexec)\b", "systemctl poweroff/reboot"),
    (_CMDPOS + r"telinit\s+[06]\b", "telinit 0/6 (shutdown/reboot)"),
]

DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b", "world/other-writable permissions"),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    (r"\bcurl\b[^\n]*\|\s*(?:ba)?sh\b", "curl piped to shell"),
    (r"\bwget\b[^\n]*\|\s*(?:ba)?sh\b", "wget piped to shell"),
    (r"\b(?:sudo\s+)?apt(?:-get)?\s+remove\b", "package removal"),
    (r"\b(?:sudo\s+)?apt(?:-get)?\s+purge\b", "package purge"),
]

_HARDLINE = [(re.compile(pattern, _RE_FLAGS), description) for pattern, description in HARDLINE_PATTERNS]
_DANGEROUS = [(re.compile(pattern, _RE_FLAGS), description) for pattern, description in DANGEROUS_PATTERNS]


def _normalize(command: str) -> str:
    return " ".join((command or "").strip().split())


def check_all_command_guards(command: str, env_type: str, approval_callback=None) -> dict:
    """Return an approval decision dict compatible with terminal toolkit_tool."""
    normalized = _normalize(command)

    for pattern_re, description in _HARDLINE:
        if pattern_re.search(normalized):
            return {
                "approved": False,
                "hardline": True,
                "message": (
                    f"BLOCKED (hardline): {description}. "
                    "This command is unconditionally blocked by the standalone terminal toolkit."
                ),
                "description": description,
            }

    for pattern_re, description in _DANGEROUS:
        if pattern_re.search(normalized):
            if approval_callback is not None and approval_callback(command, description):
                return {
                    "approved": True,
                    "user_approved": True,
                    "description": description,
                }
            return {
                "approved": False,
                "message": (
                    f"Command denied: {description}. "
                    "Pass force=True in direct Python usage or provide an approval callback."
                ),
                "description": description,
            }

    return {"approved": True}
