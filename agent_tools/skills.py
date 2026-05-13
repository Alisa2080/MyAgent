import logging
import sys
from pathlib import Path
from typing import Any

from langchain.tools import tool

from agent_core.schemas import SkillsListInput, SkillViewInput
from agent_core.workspace import WORKDIR
from agent_tools.tool_output import tool_error, tool_ok

try:
    import yaml
except ImportError:
    yaml = None


logger = logging.getLogger(__name__)

SKILLS_DIR = WORKDIR / "skills"
MAX_SKILL_CONTENT_CHARS = 50000

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    yaml_text = content[3:end].strip()
    body = content[end + len("\n---"):].strip()

    if yaml is not None:
        try:
            data = yaml.safe_load(yaml_text) or {}
            if isinstance(data, dict):
                return data, body
        except yaml.YAMLError: 
            logger.warning("Failed to parse skill frontmatter as YAML", exc_info=True)

    data: dict[str, Any] = {}
    for line in yaml_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("\"'")
    return data, body


def skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True

    if isinstance(platforms, str):
        platforms = [platforms]

    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


def iter_skill_files(skills_dir: Path = SKILLS_DIR):
    if not skills_dir.exists():
        return

    excluded = {".git", ".github", ".hub", "__pycache__"}
    for path in skills_dir.rglob("SKILL.md"):
        if any(part in excluded for part in path.parts):
            continue
        yield path


def extract_skill_description(frontmatter: dict[str, Any], body: str) -> str:
    for key in ("description", "summary"):
        desc = str(frontmatter.get(key) or "").strip()
        if desc:
            return desc[:1024]

    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:1024]
    return ""


def load_skill_metadata(skill_file: Path, skills_dir: Path = SKILLS_DIR) -> dict[str, Any] | None:
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter, body = parse_frontmatter(raw)
    if not skill_matches_platform(frontmatter):
        return None

    rel = skill_file.relative_to(skills_dir)
    parts = rel.parts
    if len(parts) >= 2:
        category = "/".join(parts[:-2]) or "general"
    else:
        category = "general"

    name = str(frontmatter.get("name") or "").strip()
    if not name:
        return None

    title = str(frontmatter.get("title") or name).strip()
    description = extract_skill_description(frontmatter, body)

    return {
        "name": name,
        "title": title,
        "description": description,
        "category": category,
        "path": skill_file,
        "dir": skill_file.parent,
    }


def _all_skills(skills_dir: Path = SKILLS_DIR) -> list[dict[str, Any]]:
    skills = []
    seen = set()
    for skill_file in iter_skill_files(skills_dir) or []:
        meta = load_skill_metadata(skill_file, skills_dir)
        if not meta:
            continue
        key = (meta["category"], meta["name"])
        if key in seen:
            continue
        seen.add(key)
        skills.append(meta)
    return sorted(skills, key=lambda item: (item["category"], item["name"]))


def build_skills_system_prompt(skills_dir: Path = SKILLS_DIR) -> str:
    skills_by_category: dict[str, list[dict[str, Any]]] = {}
    for meta in _all_skills(skills_dir):
        skills_by_category.setdefault(meta["category"], []).append(meta)

    if not skills_by_category:
        return ""

    lines = []
    for category in sorted(skills_by_category):
        lines.append(f"  {category}:")
        for skill_meta in skills_by_category[category]:
            desc = skill_meta["description"]
            if desc:
                lines.append(f"    - {skill_meta['name']}: {desc}")
            else:
                lines.append(f"    - {skill_meta['name']}")

    return (
        "## Skills\n"
        "Before answering, inspect the skills below. If a skill is relevant to the user task, "
        "load it with skill_view(name) and follow its instructions.\n\n"
        "<available_skills>\n"
        + "\n".join(lines)
        + "\n</available_skills>"
    )




def _find_skill(name: str) -> dict[str, Any] | None:
    for meta in _all_skills(SKILLS_DIR):
        skill_dir = meta["dir"]
        aliases = {
            meta["name"],
            skill_dir.name,
            str(meta["path"].relative_to(SKILLS_DIR).parent),
        }
        if name in aliases:
            return meta
    return None


@tool("skills_list", args_schema=SkillsListInput)
def skills_list(category: str = "") -> str:
    """List available skills by name, description, and category. Use skill_view to load full instructions."""
    skills = []
    categories = set()

    for meta in _all_skills(SKILLS_DIR):
        if category and meta["category"] != category:
            continue
        categories.add(meta["category"])
        skills.append(
            {
                "name": meta["name"],
                "title": meta["title"],
                "description": meta["description"],
                "category": meta["category"],
            }
        )

    return tool_ok(
        "skills_list",
        data={
            "skills": skills,
            "categories": sorted(categories),
            "count": len(skills),
        },
        message="Skills listed.",
        meta={"hint": "Use skill_view(name) to load full SKILL.md content."},
    )


@tool("skill_view", args_schema=SkillViewInput)
def skill_view(name: str, file_path: str = "") -> str:
    """Load a skill's SKILL.md content, or a supporting file under references/templates/scripts/assets."""
    meta = _find_skill(name)
    if not meta:
        return tool_error("skill_view", f"Skill not found: {name}", code="not_found")

    skill_dir = meta["dir"]

    if file_path:
        relative = Path(file_path)
        allowed_roots = {"references", "templates", "scripts", "assets"}
        if not relative.parts or relative.parts[0] not in allowed_roots:
            return tool_error(
                "skill_view",
                "file_path must be under references/, templates/, scripts/, or assets/.",
                code="invalid_path",
            )

        target = (skill_dir / relative).resolve()
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return tool_error("skill_view", "Invalid file_path.", code="invalid_path")

        if not target.exists() or not target.is_file():
            return tool_error("skill_view", f"File not found: {file_path}", code="not_found")

        return tool_ok(
            "skill_view",
            data={
                "name": meta["name"],
                "file": file_path,
                "content": target.read_text(encoding="utf-8")[:MAX_SKILL_CONTENT_CHARS],
            },
            message="Skill file loaded.",
        )

    content = meta["path"].read_text(encoding="utf-8")[:MAX_SKILL_CONTENT_CHARS]

    supporting_files = []
    for subdir in ("references", "templates", "scripts", "assets"):
        root = skill_dir / subdir
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    supporting_files.append(str(path.relative_to(skill_dir)))

    return tool_ok(
        "skill_view",
        data={
            "name": meta["name"],
            "title": meta["title"],
            "description": meta["description"],
            "category": meta["category"],
            "content": content,
            "skill_dir": str(skill_dir),
            "supporting_files": sorted(supporting_files),
        },
        message="Skill loaded.",
    )
