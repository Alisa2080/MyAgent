from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COMMAND_RE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class SkillCommand:
    command: str
    skill: dict[str, Any]
    conflict: bool = False


def normalize_skill_command(value: str) -> str:
    normalized = COMMAND_RE.sub("-", value.strip().lower()).strip("-")
    return normalized


def build_skill_command_map(
    skills: list[dict[str, Any]],
    *,
    built_in_names: set[str],
) -> dict[str, SkillCommand]:
    commands: dict[str, SkillCommand] = {}
    for skill in skills:
        names = [normalize_skill_command(str(skill.get("name") or ""))]
        if skill.get("dir"):
            names.append(normalize_skill_command(Path(str(skill["dir"])).name))
        for name in dict.fromkeys(item for item in names if item):
            if name in built_in_names or name in commands:
                continue
            commands[name] = SkillCommand(command=name, skill=skill)
    return commands


def build_skill_invocation_message(command: SkillCommand, prompt: str) -> str:
    skill = command.skill
    supporting_files = skill.get("supporting_files") or []
    file_lines = "\n".join(f"- {path}" for path in supporting_files) or "- none"
    return (
        "Use the following skill for this task.\n\n"
        f"<skill name=\"{skill.get('name') or command.command}\" dir=\"{skill.get('dir') or ''}\">\n"
        f"{skill.get('content') or ''}\n"
        "</skill>\n\n"
        "Supporting files:\n"
        f"{file_lines}\n\n"
        "User request:\n"
        f"{prompt}"
    )


def load_skill_commands(*, built_in_names: set[str]) -> dict[str, SkillCommand]:
    from agent_tools.public.skills import _all_skills

    return build_skill_command_map(_all_skills(), built_in_names=built_in_names)


def load_skill_for_command(command: SkillCommand) -> SkillCommand:
    from agent_tools.public.skills import skill_view

    result = skill_view.invoke({"name": command.skill["name"]})
    artifact = getattr(result, "artifact", None) or {}
    data = artifact.get("data") if isinstance(artifact, dict) else {}
    if not isinstance(data, dict) or not data.get("content"):
        raise ValueError(f"Skill could not be loaded: {command.skill['name']}")
    merged = {**command.skill, **data}
    return SkillCommand(command=command.command, skill=merged)
