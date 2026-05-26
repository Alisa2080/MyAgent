from agent_cli.commands import (
    COMMANDS_BY_CATEGORY,
    resolve_command,
    render_help,
    commands_for_completion,
)


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


def test_phase_1_commands_are_registered():
    help_text = render_help()

    assert "/status" in help_text
    assert "/title <name>" in help_text
    assert "/history" in help_text
    assert "/export <path.md>" in help_text


def test_commands_expose_completion_metadata():
    completion_names = [item.name for item in commands_for_completion()]

    assert "status" in completion_names
    assert "title" in completion_names
    assert "history" in completion_names
    assert "export" in completion_names
    assert resolve_command("/status").completion == "none"
    assert resolve_command("/resume").completion == "session"
    assert resolve_command("/skill").completion == "skill"
    assert resolve_command("/export").completion == "path"
