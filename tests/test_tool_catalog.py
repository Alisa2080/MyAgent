from __future__ import annotations

from types import SimpleNamespace


def _tool_names(tools):
    return [getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools]


def test_build_tools_default_matches_current_agent_tool_set():
    from agent_core.delegation import BASE_TOOLS, task
    from agent_core.tool_catalog import build_tools
    from agent_tools.public.memory import memory_manage

    expected = _tool_names([*BASE_TOOLS, memory_manage, task])

    assert _tool_names(build_tools()) == expected


def test_build_tools_includes_cron_only_when_requested():
    from agent_core.tool_catalog import build_tools

    without_cron = _tool_names(build_tools(include_cron_tools=False))
    with_cron = _tool_names(build_tools(include_cron_tools=True))

    assert "cronjob" not in without_cron
    assert "cronjob" in with_cron
    assert with_cron[:-1] == without_cron


def test_build_tools_filters_by_toolset():
    from agent_core.tool_catalog import build_tools

    assert _tool_names(build_tools(enabled_toolsets=["file_read"])) == [
        "list_directory",
        "search_files",
        "read_file",
        "file_info",
    ]
    assert _tool_names(build_tools(enabled_toolsets=["terminal"])) == [
        "terminal",
        "process",
    ]


def test_get_tool_spec_returns_metadata_by_name():
    from agent_core.tool_catalog import get_tool_spec

    spec = get_tool_spec("terminal")

    assert spec is not None
    assert spec.name == "terminal"
    assert spec.toolset == "terminal"
    assert spec.read_only is False
    assert spec.risk_level == "high"
    assert getattr(spec.tool, "name", "") == "terminal"


def test_build_tools_hides_tools_when_check_fn_fails():
    from agent_core.tool_catalog import ToolSpec, build_tools_from_specs

    fake_tool = SimpleNamespace(name="fake_tool")
    specs = [
        ToolSpec(
            name="fake_tool",
            toolset="fake",
            tool=fake_tool,
            check_fn=lambda: False,
        )
    ]

    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == []


def test_check_fn_result_is_cached_until_cleared():
    from agent_core.tool_catalog import (
        ToolSpec,
        build_tools_from_specs,
        clear_tool_catalog_cache,
    )

    clear_tool_catalog_cache()
    calls = []

    def check():
        calls.append("called")
        return True

    fake_tool = SimpleNamespace(name="cached_tool")
    specs = [
        ToolSpec(
            name="cached_tool",
            toolset="fake",
            tool=fake_tool,
            check_fn=check,
        )
    ]

    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert calls == ["called"]

    clear_tool_catalog_cache()
    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert calls == ["called", "called"]
