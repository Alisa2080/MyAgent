import pytest

from agent_cli.config_schema import (
    ConfigValidationError,
    config_to_dict,
    get_config_path_value,
    parse_config,
    set_config_path_value,
)


def test_parse_config_defaults():
    config = parse_config({})

    assert config.display.markdown == "render"
    assert config.display.theme == "default"
    assert config.model.name is None
    assert config.session.default_title == "New session"


def test_parse_config_reports_path_for_invalid_markdown():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"display": {"markdown": "weird"}})

    assert exc.value.path == "display.markdown"
    assert "raw" in exc.value.message
    assert "render" in exc.value.message
    assert "strip" in exc.value.message


def test_parse_config_rejects_unknown_path():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"unknown": True})

    assert exc.value.path == "unknown"


def test_get_and_set_config_path_value():
    data = {}
    updated = set_config_path_value(data, "display.markdown", "strip")
    config = parse_config(updated)

    assert config.display.markdown == "strip"
    assert get_config_path_value(config, "display.markdown") == "strip"
    assert config_to_dict(config)["display"]["markdown"] == "strip"
