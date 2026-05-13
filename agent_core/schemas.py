from pydantic import BaseModel, Field


class TaskInput(BaseModel):
    prompt: str = Field(
        description=(
            "The focused read-only analysis, exploration, or review task for the subagent. "
            "Do not ask it to edit files, delete files, run commands, or implement changes."
        )
    )
    description: str = Field(default="subtask", description="Short label for logs and tool output.")


class SkillsListInput(BaseModel):
    category: str = Field(
        default="",
        description="Optional exact skill category to filter by. Leave empty to list all available skills.",
    )


class SkillViewInput(BaseModel):
    name: str = Field(
        description="Skill name, skill directory name, or relative skill directory path to load.",
    )
    file_path: str = Field(
        default="",
        description=(
            "Optional supporting file path under references/, templates/, scripts/, or assets/. "
            "Leave empty to load the skill's SKILL.md instructions."
        ),
    )


class SkillManageInput(BaseModel):
    action: str = Field(
        description=(
            "Skill management action: create, patch, edit, delete, write_file, or remove_file. "
            "Use create/delete only after asking the user."
        )
    )
    name: str = Field(description="Skill name to create, patch, delete, or attach supporting files to.")
    content: str = Field(
        default="",
        description=(
            "Full SKILL.md content for create or edit. "
            "New skills must use the required procedural skill template."
        ),
    )
    category: str = Field(
        default="",
        description="Optional single-segment category directory for create, for example 'python'.",
    )
    file_path: str = Field(
        default="",
        description="Supporting file path under references/, templates/, scripts/, or assets/.",
    )
    file_content: str = Field(default="", description="Content for write_file supporting-file action.")
    old_string: str = Field(default="", description="Exact text to replace for patch.")
    new_string: str = Field(default="", description="Replacement text for patch.")
    replace_all: bool = Field(default=False, description="Replace all exact matches during patch.")
