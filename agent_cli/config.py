from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


@dataclass(frozen=True)
class RuntimeSettings:
    profile: str | None = None
    cli_home: Path | None = None
    model_name: str | None = None
    default_title: str = "New session"
    display_markdown: str = "render"
    config_path: Path | None = None
    dotenv_paths: tuple[Path, ...] = ()


PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DISPLAY_MARKDOWN_VALUES = {"render", "strip", "raw"}


@dataclass(frozen=True)
class ProfileApplication:
    profile: str | None
    cli_home: str | None
    env_value: str | None
    used_existing_home: bool


def validate_profile_name(name: str) -> str:
    if not name or not PROFILE_RE.fullmatch(name) or name == "..":
        raise ValueError("Profile name must match [A-Za-z0-9_.-]+ and cannot be '..'.")
    return name


def _profile_from_argv(argv: list[str] | None) -> str | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("-p", "--profile-short", dest="profile")
    args, _ = parser.parse_known_args(argv)
    return args.profile


def apply_profile_override(argv: list[str] | None = None) -> ProfileApplication:
    profile = _profile_from_argv(argv)
    existing = os.getenv("AGENT_CLI_HOME") or None
    if not profile:
        return ProfileApplication(None, str(Path(existing).expanduser().resolve()) if existing else None, existing, bool(existing))

    profile = validate_profile_name(profile)
    if existing:
        resolved = str(Path(existing).expanduser().resolve())
        return ProfileApplication(profile, resolved, existing, True)

    home = Path.home() / ".langchain-agent" / "profiles" / profile
    resolved = str(home.resolve())
    os.environ["AGENT_CLI_HOME"] = resolved
    return ProfileApplication(profile, resolved, resolved, False)


class ConfigError(ValueError):
    pass


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise ConfigError("PyYAML is required to read config.yaml.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")
    return data


def settings_from_config(
    *,
    cli_home: Path,
    profile: str | None,
    cli_model: str | None,
) -> RuntimeSettings:
    config_path = cli_home / "config.yaml"
    config = load_config_file(config_path)
    model_section = config.get("model") if isinstance(config.get("model"), dict) else {}
    session_section = config.get("session") if isinstance(config.get("session"), dict) else {}
    display_section = config.get("display") if isinstance(config.get("display"), dict) else {}

    default_title = str(session_section.get("default_title") or "New session")
    display_markdown = str(display_section.get("markdown") or "render")
    if display_markdown not in DISPLAY_MARKDOWN_VALUES:
        raise ConfigError(
            "display.markdown must be one of: "
            + ", ".join(sorted(DISPLAY_MARKDOWN_VALUES))
        )
    model_name = cli_model or model_section.get("name")
    if model_name is not None:
        model_name = str(model_name)

    return RuntimeSettings(
        profile=profile,
        cli_home=cli_home,
        model_name=model_name,
        default_title=default_title,
        display_markdown=display_markdown,
        config_path=config_path,
    )


def load_dotenv_files(
    *, cli_home: Path, project_root: Path, dotenv_module: Any
) -> tuple[Path, ...]:
    paths = (cli_home / ".env", project_root / ".env")
    if dotenv_module is None:
        return paths
    for path in paths:
        if path.exists():
            dotenv_module.load_dotenv(path, override=False)
    return paths
