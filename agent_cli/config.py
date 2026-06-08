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

from agent_cli.config_schema import (
    DISPLAY_MARKDOWN_VALUES,
    DISPLAY_THEME_VALUES,
    ConfigValidationError,
    config_to_dict,
    parse_config,
    set_config_path_value,
)


@dataclass(frozen=True)
class RuntimeSettings:
    profile: str | None = None
    cli_home: Path | None = None
    model_name: str | None = None
    default_title: str = "New session"
    display_markdown: str = "render"
    display_theme: str = "default"
    config_path: Path | None = None
    dotenv_paths: tuple[Path, ...] = ()
    cron_enabled: bool = True
    cron_interval_seconds: int = 60


PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    text = path.read_text(encoding="utf-8")
    if yaml is None:
        return _load_simple_yaml_mapping(path, text)
    try:
        data = yaml.safe_load(text) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")
    return data


def load_normalized_config(cli_home: Path):
    config_path = cli_home / "config.yaml"
    raw = load_config_file(config_path)
    return parse_config(raw), raw, config_path


def save_config_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is None:
        path.write_text(_dump_simple_yaml_mapping(data), encoding="utf-8")
        return
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def _load_simple_yaml_mapping(path: Path, text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_section: str | None = None

    try:
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line:
                continue
            if line.startswith("  "):
                if current_section is None:
                    raise ValueError("nested key without section")
                child = line.strip()
                key, value = _split_simple_yaml_pair(child)
                section = result.setdefault(current_section, {})
                if not isinstance(section, dict):
                    raise ValueError(f"{current_section} must be a mapping")
                section[key] = _parse_simple_yaml_scalar(value)
                continue
            if raw_line[:1].isspace():
                raise ValueError("unsupported indentation")
            key, value = _split_simple_yaml_pair(line)
            if value == "":
                result[key] = {}
                current_section = key
            else:
                result[key] = _parse_simple_yaml_scalar(value)
                current_section = None
    except ValueError as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc

    return result


def _split_simple_yaml_pair(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError("expected key: value")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    return key, value.strip()


def _parse_simple_yaml_scalar(value: str) -> Any:
    if value in {"[", "{", "]", "}"}:
        raise ValueError("unsupported YAML collection syntax")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.isdigit():
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _dump_simple_yaml_mapping(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in sorted(data):
        value = data[section]
        if isinstance(value, dict):
            lines.append(f"{section}:")
            for key in sorted(value):
                lines.append(f"  {key}: {_format_simple_yaml_scalar(value[key])}")
        else:
            lines.append(f"{section}: {_format_simple_yaml_scalar(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _format_simple_yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def settings_from_config(
    *,
    cli_home: Path,
    profile: str | None,
    cli_model: str | None,
) -> RuntimeSettings:
    try:
        parsed, _, config_path = load_normalized_config(cli_home)
    except ConfigValidationError as exc:
        raise ConfigError(str(exc)) from exc

    model_name = cli_model or parsed.model.name
    if model_name is not None:
        model_name = str(model_name)

    return RuntimeSettings(
        profile=profile,
        cli_home=cli_home,
        model_name=model_name,
        default_title=parsed.session.default_title,
        display_markdown=parsed.display.markdown,
        display_theme=parsed.display.theme,
        config_path=config_path,
        cron_enabled=parsed.cron.enabled,
        cron_interval_seconds=parsed.cron.interval_seconds,
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
