import re
from pathlib import Path

from langchain.tools import tool
from pydantic import BaseModel, Field

from agent_core.workspace import WORKDIR
from agent_tools.common import truncate
from agent_tools.hermes_shell_adapter import run_foreground_command
from agent_tools.tool_output import tool_error, tool_ok


class ExecuteCommandInput(BaseModel):
    command: str = Field(description="The shell command to execute.")


def _normalize_for_check(command: str) -> str:
    cmd = re.sub(r"\\(.)", r"\1", command)
    cmd = cmd.replace('"', "").replace("'", "")
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd.lower()


def _extract_segments(command: str) -> list[str]:
    segments = re.split(r"\s*(?:;|&&|\|\||\|)\s*", command)
    segments += re.findall(r"\$\(([^)]+)\)", command)
    segments += re.findall(r"`([^`]+)`", command)
    return [s.strip() for s in segments if s.strip()]


DANGEROUS_REGEX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\b"), "rm"),
    (re.compile(r"\brmdir\b"), "rmdir"),
    (re.compile(r"\bdel\b\s+.*[/\\][sqf]"), "del with /s /q /f"),
    (re.compile(r"\brd\b\s+.*[/\\]s"), "rd /s"),
    (re.compile(r"\bremove-item\b.*-(?:recurse|force)"), "Remove-Item -Recurse/-Force"),
    (re.compile(r"\bformat\b\s+[a-z]:"), "format drive"),
    (re.compile(r"\bmkfs\b"), "mkfs"),
    (re.compile(r"\bdd\b\s+if\s*="), "dd if="),
    (re.compile(r"\bshred\b"), "shred"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bshutdown\b"), "shutdown"),
    (re.compile(r"\breboot\b"), "reboot"),
    (re.compile(r"\bhalt\b"), "halt"),
    (re.compile(r"\binit\s+[06]\b"), "init runlevel change"),
    (re.compile(r">\s*/dev/"), "write to /dev/"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb"),
    (re.compile(r"\bchmod\s+777\b"), "chmod 777"),
    (re.compile(r"\bchown\b"), "chown"),
    (re.compile(r"(^|[\s\"'=])\.\.(?:/|\\)"), "relative path traversal outside workspace"),
    (re.compile(r"\b(?:pip|pip3|python(?:3)?\s+-m\s+pip)\s+install\b"), "pip install"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+push\b.*\s--force(?:-with-lease)?\b"), "git push --force"),
    (re.compile(r"powershell.*-(?:enc|encodedcommand)\b"), "encoded powershell"),
    (re.compile(r"\bbase64\b.*\|\s*(?:bash|sh|powershell|cmd)\b"), "base64 piped to shell"),
    (re.compile(r"\bpython(?:3)?\s+-c\b.*\b(?:urllib|requests|http\.client|socket)\b"), "python -c network access"),
    (re.compile(r"\breg\s+(?:delete|add)\b"), "registry modification"),
    (re.compile(r"\bnet\s+(?:user|localgroup)\b"), "user/group modification"),
    (re.compile(r"\btaskkill\b.*[/\\]f\b"), "taskkill /f"),
    (re.compile(r"\bkill\b.*-\s*9\b"), "kill -9"),
    (re.compile(r"\bcurl\b"), "curl (use web_fetch tool instead)"),
    (re.compile(r"\bwget\b"), "wget (use web_fetch tool instead)"),
    (re.compile(r"\binvoke-webrequest\b"), "Invoke-WebRequest (use web_fetch tool)"),
    (re.compile(r"\binvoke-restmethod\b"), "Invoke-RestMethod (use web_fetch tool)"),
    (re.compile(r"\biwr\b"), "iwr alias (use web_fetch tool)"),
    (re.compile(r"(?:^|[;&|]\s*)irm\b"), "irm alias (use web_fetch tool)"),
]


def _is_dangerous(command: str) -> str:
    normalized = _normalize_for_check(command)
    segments = _extract_segments(normalized)
    for text in [normalized, *segments]:
        for pattern, reason in DANGEROUS_REGEX:
            if pattern.search(text):
                return reason
    return ""


def _has_path_outside_workspace(command: str) -> bool:
    workdir = str(WORKDIR.resolve()).lower().replace("\\", "/").rstrip("/")
    for match in re.finditer(r"[A-Za-z]:[/\\][^\s\"'|;&>]*", command):
        try:
            path = str(Path(match.group()).resolve()).lower().replace("\\", "/").rstrip("/")
            if path != workdir and not path.startswith(workdir + "/"):
                return True
        except (ValueError, OSError):
            pass
    for match in re.finditer(r"(?<![A-Za-z:\w])(/(?!dev/)[^\s\"'|;&>]+)", command):
        try:
            path = str(Path(match.group()).resolve()).lower().replace("\\", "/").rstrip("/")
            if path != workdir and not path.startswith(workdir + "/"):
                return True
        except (ValueError, OSError):
            pass
    return False


@tool("execute_command", args_schema=ExecuteCommandInput)
def execute_command(command: str) -> str:
    """Execute a shell command inside the workspace. Returns JSON: status, message, data."""
    reason = _is_dangerous(command)
    if reason:
        return tool_error("execute_command", f"Dangerous command blocked: {reason}", code="blocked_command")
    if _has_path_outside_workspace(command):
        return tool_error("execute_command", "Command references absolute paths outside the workspace", code="invalid_path")
    payload = run_foreground_command(command, workdir=str(WORKDIR), timeout=120)
    if not isinstance(payload, dict):
        return tool_error("execute_command", "Terminal backend returned an invalid response.", code="invalid_response")

    output = str(payload.get("output") or "")
    exit_code = payload.get("exit_code", -1)
    error_message = payload.get("error")
    try:
        exit_code = int(exit_code)
    except (TypeError, ValueError):
        exit_code = -1

    stdout = output.rstrip()
    stderr = ""
    data = {
        "command": command,
        "exit_code": exit_code,
        "stdout": truncate(stdout),
        "stderr": stderr,
        "output": truncate(output) if output else "",
    }
    meta = {
        "backend": "hermes_terminal_toolkit",
        "stream_mode": "combined",
    }
    if "exit_code_meaning" in payload:
        meta["exit_code_meaning"] = payload["exit_code_meaning"]

    if error_message:
        code = "timeout" if exit_code == 124 or "timed out" in str(error_message).lower() else "command_failed"
        return tool_error("execute_command", str(error_message), code=code, data=data, meta=meta)
    if exit_code == 124:
        return tool_error("execute_command", "Timeout (120s)", code="timeout", data=data, meta=meta)
    if exit_code != 0:
        return tool_error("execute_command", f"Command exited with code {exit_code}.", code="command_failed", data=data, meta=meta)
    return tool_ok("execute_command", data=data, message="Command executed.", meta=meta)
