from __future__ import annotations

from agent_cli.commands import COMMAND_LOOKUP


def skills_handlers():
    return {"skills": handle_skills, "skill": handle_skill}


def handle_skills(ctx, arg, command):
    discovery = ctx.skill_discovery()
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


def handle_skill(ctx, arg, command):
    if not arg:
        return "Usage: /skill <name>"
    from agent_tools.public.skills import _find_skill

    meta = _find_skill(arg)
    if not meta:
        return f"Skill not found: {arg}"
    desc = meta.get("description") or ""
    return f"{meta['name']}\n{desc}".strip()
