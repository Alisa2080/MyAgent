from __future__ import annotations

from agent_cli.commands import COMMAND_LOOKUP


def skills_handlers():
    return {"skills": handle_skills, "skill": handle_skill}


def handle_skills(cli, arg, command):
    if cli.skill_discovery_provider is not None:
        discovery = cli.skill_discovery_provider()
    else:
        from agent_cli.skill_commands import load_skill_discovery

        discovery = load_skill_discovery(built_in_names=set(COMMAND_LOOKUP))
    if not discovery.entries:
        return "No skills found."
    lines = []
    for entry in discovery.entries:
        item = entry.skill
        line = f"{item['name']} - {item.get('description') or ''}".rstrip()
        if entry.command:
            line = f"{line} (/{entry.command})"
        elif entry.note:
            line = f"{line} ({entry.note})"
        lines.append(line)
    return "\n".join(lines)


def handle_skill(cli, arg, command):
    if not arg:
        return "Usage: /skill <name>"
    from agent_tools.public.skills import _find_skill

    meta = _find_skill(arg)
    if not meta:
        return f"Skill not found: {arg}"
    desc = meta.get("description") or ""
    return f"{meta['name']}\n{desc}".strip()
