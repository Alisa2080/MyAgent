import re
import shutil
from pathlib import Path

from langchain.tools import tool
from langchain_core.messages import ToolMessage

from agent_core.schemas import SkillManageInput
from agent_tools.public.skills import SKILLS_DIR, load_skill_metadata
from agent_tools.shared.tool_result import tool_failure, tool_success

VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_NAME_LENGTH = 64
MAX_SKILL_CONTENT_CHARS = 100_000
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
REQUIRED_SKILL_SECTIONS = ("## When to Use", "## Procedure", "## Verification", "## Pitfalls")

def validate_skill_name(name: str) -> str | None:
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return "Use lowercase letters, numbers, hyphens, dots, and underscores."
    return None

def validate_category(category: str | None) -> str | None:
    if not category:
        return None
    if "/" in category or "\\" in category:
        return "Category must be a single path segment."
    return validate_skill_name(category)

def validate_skill_content(content: str, *, expected_name: str = "", require_template: bool = False) -> str | None:
    if not content or not content.strip():
        return "Content cannot be empty."
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return "SKILL.md content is too large."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter."
    if "\n---" not in content[3:]:
        return "SKILL.md frontmatter is not closed."
    if "name:" not in content.split("---", 2)[1]:
        return "Frontmatter must include name."
    if "description:" not in content.split("---", 2)[1]:
        return "Frontmatter must include description."
    if require_template:
        frontmatter = content.split("---", 2)[1]
        name_match = re.search(r"(?m)^name:\s*([^\n#]+?)\s*$", frontmatter)
        if not name_match:
            return "Frontmatter must include a plain name value."
        declared_name = name_match.group(1).strip().strip("\"'")
        if expected_name and declared_name != expected_name:
            return "Frontmatter name must match the requested skill name."
        if "version:" not in frontmatter:
            return "Frontmatter must include version."
        for section in REQUIRED_SKILL_SECTIONS:
            if section not in content:
                return f"SKILL.md must include {section}."
    return None

def find_skill_dir(name: str) -> Path | None:
    for skill_file in SKILLS_DIR.rglob("SKILL.md"):
        if any(part in {".git", ".github", ".hub", "__pycache__"} for part in skill_file.parts):
            continue
        meta = load_skill_metadata(skill_file, SKILLS_DIR)
        if not meta:
            continue
        if name in {meta["name"], skill_file.parent.name}:
            return skill_file.parent
    return None

def is_local_skill_dir(skill_dir: Path) -> bool:
    try:
        skill_dir.resolve().relative_to(SKILLS_DIR.resolve())
        return True
    except ValueError:
        return False

def resolve_support_file(skill_dir: Path, file_path: str) -> Path | str:
    if not file_path:
        return "file_path is required."

    first = Path(file_path).parts[0]
    if first not in ALLOWED_SUBDIRS:
        return "file_path must be under references/, templates/, scripts/, or assets/."

    target = (skill_dir / file_path).resolve()
    try:
        target.relative_to(skill_dir.resolve())
    except ValueError:
        return "Invalid file_path."

    return target

@tool("skill_manage", args_schema=SkillManageInput)
def skill_manage(
      action: str,
      name: str,
      content: str = "",
      category: str = "",
      file_path: str = "",
      file_content: str = "",
      old_string: str = "",
      new_string: str = "",
      replace_all: bool = False,
  ) -> ToolMessage:
    """Manage reusable skills. Actions: create, patch, edit, delete, write_file, remove_file."""
    action = (action or "").strip()
    name = (name or "").strip()

    name_error = validate_skill_name(name)
    if name_error:
        return tool_failure("skill_manage", name_error, code="invalid_name")

    if action == "create":
        cat_error = validate_category(category or None)
        if cat_error:
            return tool_failure("skill_manage", cat_error, code="invalid_category")

        content_error = validate_skill_content(content, expected_name=name, require_template=True)
        if content_error:
            return tool_failure("skill_manage", content_error, code="invalid_content")

        skill_dir = SKILLS_DIR / category / name if category else SKILLS_DIR / name
        skill_file = skill_dir / "SKILL.md"

        if skill_file.exists():
            return tool_failure("skill_manage", f"Skill already exists: {name}", code="already_exists")

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")

        return tool_success(
            "skill_manage",
            data={"name": name, "path": str(skill_file)},
            message="Skill created.",
        )

    if action == "patch":
        skill_dir = find_skill_dir(name)
        if not skill_dir:
            return tool_failure("skill_manage", f"Skill not found: {name}", code="not_found")

        target = skill_dir / "SKILL.md"
        if file_path:
            resolved = resolve_support_file(skill_dir, file_path)
            if isinstance(resolved, str):
                return tool_failure("skill_manage", resolved, code="invalid_path")
            target = resolved

        if not target.exists():
            return tool_failure("skill_manage", f"File not found: {file_path or 'SKILL.md'}", code="not_found")

        if not old_string:
            return tool_failure("skill_manage", "old_string is required for patch.", code="invalid_input")
        if new_string is None:
            return tool_failure("skill_manage", "new_string is required for patch.", code="invalid_input")

        text = target.read_text(encoding="utf-8")
        count = text.count(old_string)

        if count == 0:
            return tool_failure("skill_manage", "old_string not found.", code="not_found")
        if count > 1 and not replace_all:
            return tool_failure(
                "skill_manage",
                "old_string occurs multiple times. Provide more context or set replace_all=true.",
                code="ambiguous_match",
            )

        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)

        if target.name == "SKILL.md":
            content_error = validate_skill_content(updated)
            if content_error:
                return tool_failure("skill_manage", content_error, code="invalid_content")

        target.write_text(updated, encoding="utf-8")

        return tool_success(
            "skill_manage",
            data={
                "name": name,
                "file": str(target),
                "replacements": count if replace_all else 1,
            },
            message="Skill patched.",
        )

    if action == "write_file":
        skill_dir = find_skill_dir(name)
        if not skill_dir:
            return tool_failure("skill_manage", f"Skill not found: {name}", code="not_found")

        resolved = resolve_support_file(skill_dir, file_path)
        if isinstance(resolved, str):
            return tool_failure("skill_manage", resolved, code="invalid_path")

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(file_content or "", encoding="utf-8")

        return tool_success(
            "skill_manage",
            data={"name": name, "file": str(resolved)},
            message="Supporting file written.",
        )

    if action == "remove_file":
        skill_dir = find_skill_dir(name)
        if not skill_dir:
            return tool_failure("skill_manage", f"Skill not found: {name}", code="not_found")

        resolved = resolve_support_file(skill_dir, file_path)
        if isinstance(resolved, str):
            return tool_failure("skill_manage", resolved, code="invalid_path")

        if not resolved.exists() or not resolved.is_file():
            return tool_failure("skill_manage", "File not found.", code="not_found")

        resolved.unlink()

        return tool_success(
            "skill_manage",
            data={"name": name, "file": str(resolved)},
            message="Supporting file removed.",
        )

    if action == "edit":
        skill_dir = find_skill_dir(name)
        if not skill_dir:
            return tool_failure("skill_manage", f"Skill not found: {name}", code="not_found")

        if not is_local_skill_dir(skill_dir):
            return tool_failure("skill_manage", "Only local skills can be edited.", code="access_denied")

        content_error = validate_skill_content(content)
        if content_error:
            return tool_failure("skill_manage", content_error, code="invalid_content")

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return tool_failure("skill_manage", "SKILL.md not found.", code="not_found")

        old_content = skill_file.read_text(encoding="utf-8")
        skill_file.write_text(content, encoding="utf-8")

        return tool_success(
            "skill_manage",
            data={
                "name": name,
                "path": str(skill_file),
                "old_size": len(old_content),
                "new_size": len(content),
            },
            message="Skill edited.",
        )

    if action == "delete":
        skill_dir = find_skill_dir(name)
        if not skill_dir:
            return tool_failure("skill_manage", f"Skill not found: {name}", code="not_found")

        if not is_local_skill_dir(skill_dir):
            return tool_failure("skill_manage", "Only local skills can be deleted.", code="access_denied")

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return tool_failure("skill_manage", "SKILL.md not found.", code="not_found")

        shutil.rmtree(skill_dir)

        return tool_success(
            "skill_manage",
            data={"name": name, "deleted_dir": str(skill_dir)},
            message="Skill deleted.",
        )

    return tool_failure(
        "skill_manage",
        "Unknown action. Use create, patch, edit, delete, write_file, or remove_file.",
        code="invalid_action",
    )
