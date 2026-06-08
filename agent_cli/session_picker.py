from __future__ import annotations

from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from agent_cli.rendering import format_session_picker_label


def pick_session(session_items: list[Any]) -> Any | None:
    if not session_items:
        return None

    selected_index = 0
    done = {"result": None}

    def _render_rows():
        fragments: list[tuple[str, str]] = [("", "Select a session to resume\n\n")]
        for index, item in enumerate(session_items):
            prefix = "❯ " if index == selected_index else "  "
            style = "reverse" if index == selected_index else ""
            fragments.append((style, prefix + format_session_picker_label(item) + "\n"))
        fragments.append(("", "\n↑/↓ move  Enter resume  Esc cancel"))
        return fragments

    control = FormattedTextControl(_render_rows)
    window = Window(content=control, always_hide_cursor=True)
    kb = KeyBindings()

    @kb.add("up")
    def _move_up(event):
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(session_items)
        event.app.invalidate()

    @kb.add("down")
    def _move_down(event):
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(session_items)
        event.app.invalidate()

    @kb.add("enter")
    def _accept(event):
        done["result"] = session_items[selected_index]
        event.app.exit(result=done["result"])

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    app = Application(layout=Layout(HSplit([window])), key_bindings=kb, full_screen=False)
    return app.run()
