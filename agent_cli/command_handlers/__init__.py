from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_cli.commands import CommandDef

if TYPE_CHECKING:
    from agent_cli.repl import AgentCLI

CommandHandler = Callable[["AgentCLI", str, CommandDef], str | None]


def build_command_handlers(cli: "AgentCLI") -> dict[str, CommandHandler]:
    return {
        "help": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "doctor": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "new": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "sessions": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "resume": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "status": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "title": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "history": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "export": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "clear": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "background": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "tasks": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "queue": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "steer": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "stop": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "approve": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "skills": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "skill": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "copy": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "retry": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "usage": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "exit": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
    }
