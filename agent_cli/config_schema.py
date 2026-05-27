from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from agent_cli.theme import SUPPORTED_THEMES

DISPLAY_MARKDOWN_VALUES = {"render", "strip", "raw"}
DISPLAY_THEME_VALUES = set(SUPPORTED_THEMES)


@dataclass
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
class CronConfig:
    enabled: bool = True
    interval_seconds: int = 60


@dataclass(frozen=True)
class AgentCLIConfig:
    display: DisplayConfig = DisplayConfig()
    model: ModelConfig = ModelConfig()
    session: SessionConfig = SessionConfig()
    cron: CronConfig = CronConfig()


ALLOWED_TOP_LEVEL = {"display", "model", "session", "cron"}
ALLOWED_CHILDREN = {
    "display": {"markdown", "theme"},
    "model": {"name"},
    "session": {"default_title"},
    "cron": {"enabled", "interval_seconds"},
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
    cron = _mapping(data.get("cron"), "cron")

    markdown_value = display.get("markdown", "render")
    if markdown_value is None:
        markdown_value = "render"
    markdown = str(markdown_value)
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

    title_value = session.get("default_title", "New session")
    if title_value is None:
        title_value = "New session"
    default_title = str(title_value).strip()
    if not default_title:
        raise ConfigValidationError("session.default_title", "must not be empty")

    cron_enabled_value = cron.get("enabled", True)
    if isinstance(cron_enabled_value, bool):
        cron_enabled = cron_enabled_value
    else:
        raise ConfigValidationError("cron.enabled", "must be a boolean")

    interval_value = cron.get("interval_seconds", 60)
    try:
        cron_interval_seconds = int(interval_value)
    except (TypeError, ValueError):
        raise ConfigValidationError(
            "cron.interval_seconds", "must be a positive integer"
        ) from None
    if cron_interval_seconds <= 0:
        raise ConfigValidationError(
            "cron.interval_seconds", "must be a positive integer"
        )

    return AgentCLIConfig(
        display=DisplayConfig(markdown=markdown, theme=theme),
        model=ModelConfig(name=model_name),
        session=SessionConfig(default_title=default_title),
        cron=CronConfig(
            enabled=cron_enabled,
            interval_seconds=cron_interval_seconds,
        ),
    )


def config_to_dict(config: AgentCLIConfig) -> dict[str, Any]:
    return {
        "display": {
            "markdown": config.display.markdown,
            "theme": config.display.theme,
        },
        "model": {"name": config.model.name},
        "session": {"default_title": config.session.default_title},
        "cron": {
            "enabled": config.cron.enabled,
            "interval_seconds": config.cron.interval_seconds,
        },
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
    if path == "cron.enabled":
        return config.cron.enabled
    if path == "cron.interval_seconds":
        return config.cron.interval_seconds
    raise ConfigValidationError(path, "unknown config key")


def set_config_path_value(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    if path not in {
        "display.markdown",
        "display.theme",
        "model.name",
        "session.default_title",
        "cron.enabled",
        "cron.interval_seconds",
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


def parse_config_value(raw: str) -> Any:
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    return raw


def render_config_show(config: AgentCLIConfig) -> str:
    data = config_to_dict(config)
    if yaml is None:
        lines = []
        for section, values in data.items():
            lines.append(f"{section}:")
            for key, value in values.items():
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)
    return yaml.safe_dump(data, sort_keys=True).rstrip()
