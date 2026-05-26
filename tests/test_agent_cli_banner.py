from __future__ import annotations

import pytest

from agent_cli.theme import get_theme


def test_get_theme_returns_supported_theme():
    theme = get_theme("default")

    assert theme.name == "default"
    assert theme.color_enabled is True


def test_get_theme_rejects_unknown_theme():
    with pytest.raises(ValueError, match="display.theme"):
        get_theme("weird")


def test_get_theme_rejects_empty_theme_name():
    with pytest.raises(ValueError, match="display.theme"):
        get_theme("")


def test_mono_theme_disables_color():
    assert get_theme("mono").color_enabled is False
