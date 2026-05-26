from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


CompletionKind = Literal["none", "session", "skill", "path"]


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    completion: CompletionKind = "none"

    @property
    def usage(self) -> str:
        suffix = f" {self.args_hint}" if self.args_hint else ""
        return f"/{self.name}{suffix}"


COMMAND_REGISTRY: tuple[CommandDef, ...] = (
    CommandDef("help", "Show available commands.", "Info", aliases=("h",)),
    CommandDef("new", "Start a new session.", "Session"),
    CommandDef("sessions", "List recent sessions.", "Session", aliases=("ls",)),
    CommandDef(
        "resume",
        "Resume a session.",
        "Session",
        args_hint="<session_id>",
        completion="session",
    ),
    CommandDef("status", "Show current CLI session status.", "Session"),
    CommandDef(
        "title",
        "Set the current session title.",
        "Session",
        args_hint="<name>",
    ),
    CommandDef("history", "Show user and assistant messages.", "Session"),
    CommandDef(
        "export",
        "Export user and assistant messages to Markdown.",
        "Session",
        args_hint="<path.md>",
        completion="path",
    ),
    CommandDef("clear", "Clear the terminal screen.", "Session"),
    CommandDef("skills", "List available local skills.", "Skills"),
    CommandDef(
        "skill",
        "Show a skill summary.",
        "Skills",
        args_hint="<name>",
        completion="skill",
    ),
    CommandDef("exit", "Exit the CLI.", "Exit", aliases=("quit", "q")),
)


def _names_for(command: CommandDef) -> Iterable[str]:
    yield command.name
    for alias in command.aliases:
        yield alias


COMMAND_LOOKUP: dict[str, CommandDef] = {
    name: command
    for command in COMMAND_REGISTRY
    for name in _names_for(command)
}

COMMANDS_BY_CATEGORY: dict[str, list[CommandDef]] = {}
for _command in COMMAND_REGISTRY:
    COMMANDS_BY_CATEGORY.setdefault(_command.category, []).append(_command)


def normalize_command_name(raw: str) -> str:
    return raw.strip().split(maxsplit=1)[0].lstrip("/").lower()


def resolve_command(raw: str) -> CommandDef | None:
    if not raw.strip():
        return None
    return COMMAND_LOOKUP.get(normalize_command_name(raw))


def commands_for_completion() -> tuple[CommandDef, ...]:
    return COMMAND_REGISTRY


def render_help() -> str:
    lines = ["Available commands:"]
    for category, commands in COMMANDS_BY_CATEGORY.items():
        lines.append("")
        lines.append(f"{category}:")
        for command in commands:
            lines.append(f"  {command.usage:<24} {command.description}")
    return "\n".join(lines)
