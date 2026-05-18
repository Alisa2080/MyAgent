from dataclasses import dataclass, field
from datetime import datetime
import platform
from pathlib import Path
from typing import Sequence

from agent_tools.skills import build_skills_system_prompt


PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 20_000


@dataclass(frozen=True)
class SystemPromptContext:
    workdir: Path
    model_name: str
    current_datetime: str
    platform: str
    memory_blocks: Sequence[str] = field(default_factory=tuple)
    project_instruction_blocks: Sequence[str] = field(default_factory=tuple)


class SystemPromptBuilder:
    """Build system prompts from stable sections and runtime context."""

    def build_parent(self, context: SystemPromptContext) -> str:
        stable_sections = [
            self._build_core(),
            self._build_delegation_policy(),
            self._build_filesystem_policy(),
            self._build_memory_policy(),
            self._build_skill_policy(),
            self._build_skill_template_policy(),
            self._build_skills_section(),
            self._build_project_instructions_section(context.project_instruction_blocks),
            self._build_memory_section(context.memory_blocks),
            self._build_language_policy(),
        ]
        dynamic_sections = [
            self._build_dynamic_context(context),
        ]
        return self._join_sections(
            [
                self._join_sections(stable_sections),
                self._build_dynamic_boundary(),
                self._join_sections(dynamic_sections),
            ]
        )

    def build_subagent(self, context: SystemPromptContext) -> str:
        stable_sections = [
            self._build_subagent_core(),
            self._build_subagent_filesystem_policy(),
            self._build_skill_policy(),
            self._build_skills_section(),
            self._build_project_instructions_section(context.project_instruction_blocks),
        ]
        dynamic_sections = [
            self._build_dynamic_context(context),
        ]
        return self._join_sections(
            [
                self._join_sections(stable_sections),
                self._build_dynamic_boundary(),
                self._join_sections(dynamic_sections),
            ]
        )

    def _build_core(self) -> str:
        return "You are a coding agent."

    def _build_delegation_policy(self) -> str:
        return (
            "Use the task tool to delegate focused read-only exploration, analysis, or review subtasks when that helps. "
            "Each task tool call starts a fresh-context subagent that shares the filesystem but not the conversation "
            "history, and that subagent cannot edit files, delete files, or execute shell commands. "
            "Do not delegate implementation, test execution, or workspace mutation to task; perform those actions "
            "yourself with the direct tools so approval controls apply. "
            "You may call multiple task tools for independent read-only subtasks, then synthesize their returned "
            "summaries yourself. Use direct tools yourself when delegation is unnecessary."
        )

    def _build_filesystem_policy(self) -> str:
        return (
            "For filesystem work, prefer purpose-built tools: use search_files to find files by path/name, "
            "search_files target='content' to search file contents, read_file to inspect files, patch for targeted "
            "edits or structured multi-file changes, and write_file only for full-file writes. "
            "Use terminal(background=False) for running tests, builds, package scripts, or commands that need a "
            "shell. Use terminal(background=True) for long-running servers, watchers, or jobs, then use "
            "process(action='poll'), process(action='log'), process(action='wait'), or process(action='kill') "
            "to manage the returned session_id. Use pty=True only for interactive CLI tools or REPL-like commands. "
            "Do not use terminal for routine ls/find/grep/rg/cat file exploration when filesystem tools can do it."
        )

    def _build_memory_policy(self) -> str:
        return (
            "Use memory_manage only for compact durable facts that should survive future sessions, such as user "
            "preferences, corrections, stable project conventions, environment facts, or tool quirks. Do not save "
            "task progress, raw logs, temporary TODO state, secrets, credentials, or facts that are cheap to "
            "rediscover. Memory writes persist to disk immediately but do not change the current session's frozen "
            "system prompt; they become available after the next agent session starts."
        )

    def _build_skill_policy(self) -> str:
        return (
            "Available skills are listed below. If a skill is relevant and you need its exact instructions, call "
            "skill_view with the exact skill name before relying on it. Skills are procedural memory: reusable "
            "methods for recurring task classes."
        )

    def _build_skill_template_policy(self) -> str:
        return (
            "Use skill_manage only when the conversation reveals a durable workflow, correction, pitfall, command "
            "sequence, API usage pattern, or user-preferred procedure. Do not save temporary task progress, one-off "
            "facts, private secrets, raw logs, or ordinary user preferences as skills. Before creating a new skill, "
            "prefer patching an existing relevant skill. Create a new skill only when no existing skill covers the "
            "workflow. Ask the user before creating or deleting a skill. You may patch a skill after using it if you "
            "discovered missing steps, wrong commands, or important pitfalls. Use patch for small changes. Use edit "
            "only for major rewrites after reading the existing skill with skill_view. Never delete a skill unless "
            "the user explicitly asks you to delete it. Before delete, confirm the exact skill name with the user. "
            "When creating a skill, use exactly this structure:\n\n"
            "---\n"
            "name: example-skill\n"
            "description: Short description of when and how to use this skill.\n"
            "version: 1.0.0\n"
            "---\n\n"
            "# Example Skill\n\n"
            "## When to Use\n"
            "- ...\n\n"
            "## Procedure\n"
            "1. ...\n\n"
            "## Verification\n"
            "- ...\n\n"
            "## Pitfalls\n"
            "- ..."
        )

    def _build_skills_section(self) -> str:
        return build_skills_system_prompt()

    def _build_project_instructions_section(self, instruction_blocks: Sequence[str]) -> str:
        if not instruction_blocks:
            return ""
        return self._join_sections(["## Project Instructions", *instruction_blocks])

    def _build_memory_section(self, memory_blocks: Sequence[str]) -> str:
        return self._join_sections(memory_blocks)

    def _build_language_policy(self) -> str:
        return "Answer the user in their language unless they ask otherwise."

    def _build_subagent_core(self) -> str:
        return (
            "You are a read-only coding subagent. Complete only the analysis, exploration, or review task you were "
            "given. You cannot edit files, delete files, write durable memory, or execute shell commands."
        )

    def _build_subagent_filesystem_policy(self) -> str:
        return (
            "For filesystem work, use search_files for file discovery, search_files target='content' for content "
            "search, and read_file for inspecting file contents. Use tools as needed, then finish with a concise "
            "plain-text summary of findings, risks, and suggested next steps. Do not spawn other subagents."
        )

    def _build_dynamic_boundary(self) -> str:
        return "===== DYNAMIC RUNTIME CONTEXT: may change between runs ====="

    def _build_dynamic_context(self, context: SystemPromptContext) -> str:
        return (
            "## Runtime Context\n"
            f"- current_datetime: {context.current_datetime}\n"
            f"- workdir: {context.workdir}\n"
            f"- model: {context.model_name}\n"
            f"- platform: {context.platform}"
        )

    def _join_sections(self, sections: Sequence[str]) -> str:
        return "\n\n".join(section.strip() for section in sections if section and section.strip())


def build_prompt_context(
    *,
    workdir: Path,
    model_name: str,
    memory_blocks: Sequence[str] = (),
    project_instruction_blocks: Sequence[str] = (),
) -> SystemPromptContext:
    return SystemPromptContext(
        workdir=workdir,
        model_name=model_name,
        current_datetime=datetime.now().astimezone().isoformat(timespec="seconds"),
        platform=platform.platform(),
        memory_blocks=tuple(memory_blocks),
        project_instruction_blocks=tuple(project_instruction_blocks),
    )


def load_project_instruction_blocks(workdir: Path) -> tuple[str, ...]:
    blocks = []
    for filename in PROJECT_INSTRUCTION_FILES:
        path = workdir / filename
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            continue
        if len(content) > MAX_PROJECT_INSTRUCTION_CHARS:
            content = content[:MAX_PROJECT_INSTRUCTION_CHARS].rstrip() + "\n\n[truncated]"
        blocks.append(f"### {filename}\n{content}")
    return tuple(blocks)


def model_display_name(model: object) -> str:
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if value:
            return str(value)
    return type(model).__name__
