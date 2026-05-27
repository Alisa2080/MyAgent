from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_cli.theme import SUPPORTED_THEMES

DISPLAY_MARKDOWN_VALUES = {"render", "strip", "raw"}
DISPLAY_THEME_VALUES = set(SUPPORTED_THEMES)


@dataclass(frozen=True)
class ConfigValidationError(ValueError):
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class DisplayConfig:
    markdown: str = "render"
    theme: str = "default"


@dataclass(frozen=True)
class ModelConfig:
    name: str | None = None


@dataclass(frozen=True)
class SessionConfig:
    default_title: str = "New session"


@dataclass(frozen=True)
class AgentCLIConfig:
    display: DisplayConfig = DisplayConfig()
    model: ModelConfig = ModelConfig()
    session: SessionConfig = SessionConfig()


ALLOWED_TOP_LEVEL = {"display", "model", "session"}
ALLOWED_CHILDREN = {
    "display": {"markdown", "theme"},
    "model": {"name"},
    "session": {"default_title"},
}


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigValidationError(path, "must be a mapping")
    return value


def _reject_unknown(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key not in ALLOWED_TOP_LEVEL:
            raise ConfigValidationError(str(key), "unknown config key")
        section = _mapping(value, str(key))
        for child in section:
            if child not in ALLOWED_CHILDREN[key]:
                raise ConfigValidationError(f"{key}.{child}", "unknown config key")


def parse_config(data: dict[str, Any]) -> AgentCLIConfig:
    _reject_unknown(data)
    display = _mapping(data.get("display"), "display")
    model = _mapping(data.get("model"), "model")
    session = _mapping(data.get("session"), "session")

    markdown = str(display.get("markdown") or "render")
    if markdown not in DISPLAY_MARKDOWN_VALUES:
        allowed = ", ".join(sorted(DISPLAY_MARKDOWN_VALUES))
        raise ConfigValidationError("display.markdown", f"must be one of: {allowed}")

    raw_theme = display.get("theme", "default")
    theme = "default" if raw_theme is None else str(raw_theme)
    if theme not in DISPLAY_THEME_VALUES:
        allowed = ", ".join(sorted(DISPLAY_THEME_VALUES))
        raise ConfigValidationError("display.theme", f"must be one of: {allowed}")

    model_name = model.get("name")
    if model_name is not None:
        model_name = str(model_name)

    default_title = str(session.get("default_title") or "New session").strip()
    if not default_title:
        raise ConfigValidationError("session.default_title", "must not be empty")

    return AgentCLIConfig(
        display=DisplayConfig(markdown=markdown, theme=theme),
        model=ModelConfig(name=model_name),
        session=SessionConfig(default_title=default_title),
    )


def config_to_dict(config: AgentCLIConfig) -> dict[str, Any]:
    return {
        "display": {
            "markdown": config.display.markdown,
            "theme": config.display.theme,
        },
        "model": {"name": config.model.name},
        "session": {"default_title": config.session.default_title},
    }


def get_config_path_value(config: AgentCLIConfig, path: str) -> Any:
    if path == "display.markdown":
        return config.display.markdown
    if path == "display.theme":
        return config.display.theme
    if path == "model.name":
        return config.model.name
    if path == "session.default_title":
        return config.session.default_title
    raise ConfigValidationError(path, "unknown config key")


def set_config_path_value(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    if path not in {
        "display.markdown",
        "display.theme",
        "model.name",
        "session.default_title",
    }:
        raise ConfigValidationError(path, "unknown config key")
    section, key = path.split(".", 1)
    updated = {name: dict(raw) if isinstance(raw, dict) else raw for name, raw in data.items()}
    section_data = updated.get(section)
    if section_data is None:
        section_data = {}
    if not isinstance(section_data, dict):
        raise ConfigValidationError(section, "must be a mapping")
    section_data[key] = value
    updated[section] = section_data
    parse_config(updated)
    return updated
