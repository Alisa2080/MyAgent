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
    "echo",
    "printf",
    "true",
    "false",
    "wc",
    "which",
}
_READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show"}
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
_GIT_BRANCH_DELETE_FLAGS = {"-d", "-D", "--delete"}
_GIT_BRANCH_MUTATION_FLAGS = {
    "-m",
    "-M",
    "--move",
    "-c",
    "-C",
    "--copy",
    "-u",
    "--set-upstream-to",
    "--unset-upstream",
}
_SED_WRITE_SCRIPT = re.compile(r"(^|[;{\s])(?:[0-9,$!]+)?w(?:\s|/|$)")


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _contains_complex_shell(command: str) -> bool:
    return bool(re.search(r"(\$\(|`|<<|;|(?<!\|)\|(?!\|)|\|\||&&|\n|\bfor\b|\bwhile\b)", command))


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
    if base == "command":
        return len(tokens) > 2 and tokens[1] in {"-v", "-V"}
    if base == "sed":
        return len(tokens) > 1 and tokens[1] == "-n" and not _sed_has_write_command(tokens)
    if base == "git":
        return len(tokens) > 1 and (tokens[1] in _READ_ONLY_GIT_SUBCOMMANDS or tokens[1:] == ["branch"])
    if base in _READ_ONLY_COMMANDS:
        return True
    if len(tokens) >= 2 and tokens[1] in _INFO_FLAGS:
        return True
    return False


def _unwrap_command_builtin(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] != "command" or _is_read_only(tokens):
        return tokens
    index = 1
    while index < len(tokens) and tokens[index] == "-p":
        index += 1
    return tokens[index:]


def _sed_has_write_command(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "sed":
        return False

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-n", "--quiet", "--silent"}:
            index += 1
            continue
        if token == "-e":
            if index + 1 < len(tokens) and _SED_WRITE_SCRIPT.search(tokens[index + 1]):
                return True
            index += 2
            continue
        if token.startswith("-e") and len(token) > 2:
            if _SED_WRITE_SCRIPT.search(token[2:]):
                return True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return bool(_SED_WRITE_SCRIPT.search(token))
    return False


def _find_exec_tokens(tokens: list[str]) -> list[str]:
    for flag in ("-exec", "-execdir"):
        if flag not in tokens:
            continue
        index = tokens.index(flag) + 1
        command_tokens: list[str] = []
        while index < len(tokens) and tokens[index] not in {";", "+"}:
            command_tokens.append(tokens[index])
            index += 1
        return command_tokens
    return []


def _has_git_branch_delete(tokens: list[str]) -> bool:
    return len(tokens) > 2 and tokens[:2] == ["git", "branch"] and any(
        token in _GIT_BRANCH_DELETE_FLAGS or token.startswith("--delete=") for token in tokens[2:]
    )


def _has_git_branch_mutation(tokens: list[str]) -> bool:
    if len(tokens) <= 2 or tokens[:2] != ["git", "branch"]:
        return False
    if _has_git_branch_delete(tokens):
        return False
    if any(
        token in _GIT_BRANCH_MUTATION_FLAGS
        or token.startswith("--set-upstream-to=")
        or token.startswith("--unset-upstream=")
        for token in tokens[2:]
    ):
        return True
    return any(not token.startswith("-") for token in tokens[2:])


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
    risk_tokens = _unwrap_command_builtin(tokens)
    base = risk_tokens[0] if risk_tokens else ""
    find_exec_tokens = _find_exec_tokens(tokens)
    find_exec_base = find_exec_tokens[0] if find_exec_tokens else ""

    if background:
        risk_tags.append("long_running_process")
    if _contains_write_redirect(command) or _sed_has_write_command(tokens):
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
    if tokens and tokens[0] == "find" and "-delete" in tokens:
        risk_tags.append("destructive_command")
    if find_exec_base in _DELETE_COMMANDS:
        risk_tags.append("destructive_command")
    if find_exec_base in _WRITE_COMMANDS:
        risk_tags.append("filesystem_write")
    if find_exec_base and find_exec_base not in _DELETE_COMMANDS | _WRITE_COMMANDS:
        risk_tags.append("complex_shell")
    if _has_git_branch_delete(tokens):
        risk_tags.append("destructive_command")
    if _has_git_branch_mutation(tokens):
        risk_tags.append("filesystem_write")
    if not risk_tags and _contains_complex_shell(command):
        risk_tags.append("complex_shell")
    if not risk_tags and not (_is_read_only(tokens) or _is_test_command(tokens)):
        risk_tags.append("unknown_shell")

    if risk_tags:
        unique_risks = tuple(dict.fromkeys(risk_tags))
        return PolicyDecision.review(
            unique_risks[0],
            risk_tags=unique_risks,
            requires_network=bool({"network_access", "package_install"} & set(unique_risks)),
        )

    return PolicyDecision.allow("low_risk_command")
