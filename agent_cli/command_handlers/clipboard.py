from __future__ import annotations

import sys

from agent_cli.text_input import format_osc52


def clipboard_handlers():
    return {"copy": handle_copy}


def handle_copy(ctx, arg, command):
    index = 1
    if arg:
        try:
            index = int(arg)
        except ValueError:
            return "Usage: /copy [N]"
    if index < 1:
        return "Usage: /copy [N]"
    if index > len(ctx.assistant_replies):
        return "No assistant reply available to copy."
    text = ctx.assistant_replies[-index]
    sys.stdout.write(format_osc52(text))
    sys.stdout.flush()
    return f"Copied assistant reply {index}."
