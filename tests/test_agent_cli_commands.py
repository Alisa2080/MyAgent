from agent_cli.commands import (
    COMMANDS_BY_CATEGORY,
    resolve_command,
    render_help,
    commands_for_completion,
)
from agent_cli.skill_commands import (
    SkillCommand,
    build_skill_command_map,
    build_skill_discovery,
    build_skill_invocation_message,
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


def test_phase_3_background_commands_are_registered():
    assert resolve_command("/background").name == "background"
    assert resolve_command("/tasks").name == "tasks"
    assert resolve_command("/agents").name == "tasks"
    assert resolve_command("/queue").name == "queue"
    assert resolve_command("/steer").name == "steer"
    assert resolve_command("/stop").name == "stop"
    assert resolve_command("/approve").name == "approve"

    help_text = render_help()
    assert "/background <prompt>" in help_text
    assert "/tasks" in help_text
    assert "/steer <task_id> <message>" in help_text


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


def test_build_skill_command_map_registers_skill_name_and_dir_alias():
    skills = [
        {
            "name": "Python Debug",
            "description": "Debug Python failures.",
            "path": "/repo/skills/python-debug/SKILL.md",
            "dir": "/repo/skills/python-debug",
        }
    ]

    commands = build_skill_command_map(skills, built_in_names={"help"})

    assert commands["python-debug"].skill["name"] == "Python Debug"


def test_build_skill_command_map_builtin_conflict_is_not_registered():
    skills = [
        {
            "name": "help",
            "description": "conflict",
            "path": "/repo/skills/help/SKILL.md",
            "dir": "/repo/skills/help",
        }
    ]

    commands = build_skill_command_map(skills, built_in_names={"help"})

    assert commands == {}


def test_build_skill_discovery_reports_registered_and_conflicting_skills():
    skills = [
        {
            "name": "python-debug",
            "description": "Debug Python failures.",
            "path": "/repo/skills/python-debug/SKILL.md",
            "dir": "/repo/skills/python-debug",
        },
        {
            "name": "help",
            "description": "conflict",
            "path": "/repo/skills/help/SKILL.md",
            "dir": "/repo/skills/help",
        },
    ]

    discovery = build_skill_discovery(skills, built_in_names={"help"})

    assert discovery.commands["python-debug"].command == "python-debug"
    assert discovery.entries[0].command == "python-debug"
    assert discovery.entries[1].command is None
    assert "conflicts" in discovery.entries[1].note


def test_build_skill_discovery_builtin_name_conflict_does_not_fall_back_to_dir_alias():
    skills = [
        {
            "name": "help",
            "description": "conflict",
            "path": "/repo/skills/custom-help/SKILL.md",
            "dir": "/repo/skills/custom-help",
        }
    ]

    discovery = build_skill_discovery(skills, built_in_names={"help"})

    assert discovery.commands == {}
    assert discovery.entries[0].command is None
    assert "conflicts with built-in command /help" == discovery.entries[0].note


def test_build_skill_invocation_message_includes_content_and_supporting_files():
    command = SkillCommand(
        command="python-debug",
        skill={
            "name": "python-debug",
            "title": "Python Debug",
            "description": "Debug Python.",
            "dir": "/repo/skills/python-debug",
            "content": "# Python Debug\nUse pytest.",
            "supporting_files": ["references/example.md"],
        },
    )

    message = build_skill_invocation_message(command, "fix traceback")

    assert '<skill name="python-debug" dir="/repo/skills/python-debug">' in message
    assert "# Python Debug" in message
    assert "- references/example.md" in message
    assert "User request:\nfix traceback" in message


def test_readme_agent_cli_docs_cover_current_capabilities():
    from pathlib import Path

    text = Path("README.md").read_text(encoding="utf-8")

    assert "--profile" in text
    assert "config.yaml" in text
    assert "/background" in text
    assert "/tasks" in text
    assert "/queue" in text
    assert "/steer" in text
    assert "/approve" in text
    assert "/stop" in text
    assert "logs" in text.lower()
    assert "OPENAI_API_KEY" in text
    assert "edit JSON" in text or "edit" in text


def test_every_builtin_command_has_handler_key():
    from agent_cli.commands import COMMAND_REGISTRY

    for command in COMMAND_REGISTRY:
        assert command.effective_handler_key
        assert command.effective_handler_key == (command.handler_key or command.name)


def test_aliases_resolve_to_canonical_handler_key():
    from agent_cli.commands import resolve_command

    command = resolve_command("/h")
    assert command is not None
    assert command.name == "help"
    assert command.effective_handler_key == "help"


def test_cron_command_is_registered_and_in_help():
    from agent_cli.commands import COMMAND_LOOKUP, render_help

    assert "cron" in COMMAND_LOOKUP
    help_text = render_help()
    assert "/cron" in help_text
    assert "Manage scheduled cron jobs." in help_text
