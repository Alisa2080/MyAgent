from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_cli.commands import CommandDef
from agent_cli.command_handlers.background import background_handlers
from agent_cli.command_handlers.clipboard import clipboard_handlers
from agent_cli.command_handlers.cron import cron_handlers
from agent_cli.command_handlers.debug import debug_handlers
from agent_cli.command_handlers.gateway import gateway_handlers
from agent_cli.command_handlers.session import session_handlers
from agent_cli.command_handlers.skills import skills_handlers

if TYPE_CHECKING:
    from agent_cli.command_context import CommandContext

CommandHandler = Callable[["CommandContext", str, CommandDef], str | None]


def build_command_handlers(ctx: "CommandContext") -> dict[str, CommandHandler]:
    handlers: dict[str, CommandHandler] = {}
    for group in (
        debug_handlers(),
        session_handlers(),
        background_handlers(),
        gateway_handlers(),
        cron_handlers(),
        skills_handlers(),
        clipboard_handlers(),
    ):
        handlers.update(group)
    return handlers
