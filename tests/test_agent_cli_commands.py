from agent_cli.commands import COMMANDS_BY_CATEGORY, resolve_command, render_help


def test_resolve_command_handles_slash_and_alias():
    assert resolve_command("/help").name == "help"
    assert resolve_command("quit").name == "exit"
    assert resolve_command("/q").name == "exit"


def test_resolve_command_returns_none_for_unknown():
    assert resolve_command("/missing") is None


def test_render_help_lists_core_commands():
    help_text = render_help()
    assert "/help" in help_text
    assert "/sessions" in help_text
    assert "/resume <session_id>" in help_text


def test_commands_group_by_category():
    assert "Session" in COMMANDS_BY_CATEGORY
    assert any(cmd.name == "new" for cmd in COMMANDS_BY_CATEGORY["Session"])
