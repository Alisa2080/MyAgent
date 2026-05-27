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


def test_render_banner_uses_boxed_console_for_wide_terminal():
    from agent_cli.banner import BannerContext, render_banner

    context = BannerContext(
        workdir="/repo",
        profile="dev",
        home="/home/user/.langchain-agent/profiles/dev",
        model="gpt-5",
        session="s1",
        background_counts={"running": 2, "waiting_approval": 1},
    )

    output = render_banner(context, width=100, theme_name="mono")

    assert "┌ Agent CLI" in output
    assert "profile  dev" in output
    assert "/background /tasks /steer /stop /approve" in output
    assert "RUN 2" in output
    assert "WAIT 1" in output


def test_render_banner_uses_narrow_fallback():
    from agent_cli.banner import BannerContext, render_banner

    context = BannerContext(
        workdir="/repo",
        profile="dev",
        home="/home/user/.langchain-agent/profiles/dev",
        model="gpt-5",
        session="s1",
        background_counts={"running": 2, "waiting_approval": 1},
    )

    output = render_banner(context, width=40, theme_name="mono")

    assert "Agent CLI" in output
    assert "session s1" in output
    assert "┌" not in output
