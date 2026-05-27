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


def test_parse_config_rejects_explicit_empty_markdown():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"display": {"markdown": ""}})

    assert exc.value.path == "display.markdown"


def test_parse_config_rejects_boolean_markdown():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"display": {"markdown": False}})

    assert exc.value.path == "display.markdown"


def test_parse_config_rejects_empty_default_title():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"session": {"default_title": ""}})

    assert exc.value.path == "session.default_title"


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


def test_parse_config_includes_cron_defaults():
    config = parse_config({})

    assert config.cron.enabled is True
    assert config.cron.interval_seconds == 60


def test_parse_config_accepts_cron_values():
    config = parse_config({"cron": {"enabled": False, "interval_seconds": 5}})

    assert config.cron.enabled is False
    assert config.cron.interval_seconds == 5


def test_parse_config_rejects_invalid_cron_interval():
    import pytest

    from agent_cli.config_schema import ConfigValidationError, parse_config

    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"cron": {"interval_seconds": 0}})

    assert str(exc.value) == "cron.interval_seconds: must be a positive integer"


@pytest.mark.parametrize("value", [True, False, 1.5, "1.5", "abc"])
def test_parse_config_rejects_non_integer_cron_interval(value):
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"cron": {"interval_seconds": value}})

    assert str(exc.value) == "cron.interval_seconds: must be a positive integer"


def test_parse_config_rejects_unknown_cron_key():
    import pytest

    from agent_cli.config_schema import ConfigValidationError, parse_config

    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"cron": {"weird": True}})

    assert str(exc.value) == "cron.weird: unknown config key"


def test_config_get_and_to_dict_include_cron():
    from agent_cli.config_schema import config_to_dict, get_config_path_value, parse_config

    config = parse_config({"cron": {"enabled": False, "interval_seconds": 12}})

    assert get_config_path_value(config, "cron.enabled") is False
    assert get_config_path_value(config, "cron.interval_seconds") == 12
    assert config_to_dict(config)["cron"] == {
        "enabled": False,
        "interval_seconds": 12,
    }
