from __future__ import annotations

import re
import shlex

from agent_core.permissions.models import PolicyDecision
from agent_tools.hermes_terminal_toolkit.approval import check_all_command_guards


_READ_ONLY_COMMANDS = {
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "which",
    "command",
}
_READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch"}
_TEST_COMMANDS = {
    ("pytest",),
    ("python", "-m", "pytest"),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("cargo", "test"),
    ("go", "test"),
}
_INFO_FLAGS = {"--version", "-V", "version"}

_WRITE_COMMANDS = {"cp", "mv", "mkdir", "touch"}
_DELETE_COMMANDS = {"rm"}
_PERMISSION_COMMANDS = {"chmod", "chown"}
_PACKAGE_INSTALL_PATTERNS = (
    ("pip", "install"),
    ("python", "-m", "pip", "install"),
    ("npm", "install"),
    ("pnpm", "install"),
    ("yarn", "add"),
    ("apt", "install"),
    ("apt-get", "install"),
    ("brew", "install"),
)
_NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp"}
_NETWORK_GIT_SUBCOMMANDS = {"clone", "fetch", "pull", "push"}
_SERVICE_COMMANDS = {"systemctl", "service"}
_LONG_RUNNING_TOKENS = {"vite", "uvicorn", "watch"}


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _contains_complex_shell(command: str) -> bool:
    return bool(re.search(r"(\$\(|`|<<|;|\|\||&&|\n|\bfor\b|\bwhile\b)", command))


def _contains_write_redirect(command: str) -> bool:
    return bool(re.search(r"(^|[^<])>{1,2}($|[^>])", command)) or bool(re.search(r"\btee\b", command))


def _starts_with(tokens: list[str], prefix: tuple[str, ...]) -> bool:
    return tuple(tokens[: len(prefix)]) == prefix


def _is_test_command(tokens: list[str]) -> bool:
    return any(_starts_with(tokens, prefix) for prefix in _TEST_COMMANDS)


def _is_read_only(tokens: list[str]) -> bool:
    if not tokens:
        return False
    base = tokens[0]
    if base == "sed":
        return len(tokens) > 1 and tokens[1] == "-n"
    if base == "git":
        return len(tokens) > 1 and tokens[1] in _READ_ONLY_GIT_SUBCOMMANDS
    if base in _READ_ONLY_COMMANDS:
        return True
    if len(tokens) >= 2 and tokens[1] in _INFO_FLAGS:
        return True
    return False


def classify_command(command: str, *, background: bool = False) -> PolicyDecision:
    guard = check_all_command_guards(command, env_type="local")
    if guard.get("hardline"):
        return PolicyDecision.deny(
            "hardline_destructive_command",
            risk_tags=("hardline_destructive_command",),
            message=guard.get("message", "Command is unconditionally blocked."),
        )

    risk_tags: list[str] = []
    tokens = _tokens(command)
    base = tokens[0] if tokens else ""

    if background:
        risk_tags.append("long_running_process")
    if _contains_write_redirect(command):
        risk_tags.append("write_redirect")
    if base in _WRITE_COMMANDS:
        risk_tags.append("filesystem_write")
    if base in _DELETE_COMMANDS:
        risk_tags.append("destructive_command")
    if base in _PERMISSION_COMMANDS:
        risk_tags.append("permission_change")
    if any(_starts_with(tokens, prefix) for prefix in _PACKAGE_INSTALL_PATTERNS):
        risk_tags.append("package_install")
    if base in _NETWORK_COMMANDS or (base == "git" and len(tokens) > 1 and tokens[1] in _NETWORK_GIT_SUBCOMMANDS):
        risk_tags.append("network_access")
    if base == "sudo":
        risk_tags.append("privilege_escalation")
    if base in _SERVICE_COMMANDS:
        risk_tags.append("service_control")
    if base in _LONG_RUNNING_TOKENS or tuple(tokens[:3]) == ("npm", "run", "dev"):
        risk_tags.append("long_running_process")
    if not risk_tags and _contains_complex_shell(command):
        risk_tags.append("complex_shell")
    if not risk_tags and not (_is_read_only(tokens) or _is_test_command(tokens)):
        risk_tags.append("unknown_shell")

    if risk_tags:
        unique_risks = tuple(dict.fromkeys(risk_tags))
        return PolicyDecision.review(
            unique_risks[0],
            risk_tags=unique_risks,
            requires_network="network_access" in unique_risks,
        )

    return PolicyDecision.allow("low_risk_command")
