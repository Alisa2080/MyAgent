from __future__ import annotations

from types import SimpleNamespace


def _tool_names(tools):
    return [getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools]


def test_build_tools_default_matches_current_agent_tool_set():
    from agent_core.delegation import BASE_TOOLS, task
    from agent_core.tool_catalog import build_tools
    from agent_tools.public.code_execution import execute_code
    from agent_tools.public.memory import memory_manage

    # execute_code comes after process (terminal tools) in the spec list
    expected = [
        "list_directory", "search_files", "read_file", "file_info",
        "web_search", "web_extract", "skills_list", "skill_view",
        "write_file", "patch", "terminal", "process",
        "execute_code",
        "skill_manage", "clarify",
        "memory_manage", "task",
    ]

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


def test_build_tools_respects_enabled_by_default_for_explicit_toolsets():
    from agent_core.tool_catalog import ToolSpec, build_tools_from_specs

    fake_tool = SimpleNamespace(name="disabled_tool")
    specs = [
        ToolSpec(
            name="disabled_tool",
            toolset="fake",
            tool=fake_tool,
            enabled_by_default=False,
        )
    ]

    # When explicit toolsets are provided, enabled_by_default is ignored
    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    # When no explicit toolsets, enabled_by_default=False filters it out
    assert build_tools_from_specs(specs) == []


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


def test_code_execution_defaults_enabled_for_dev_and_test(monkeypatch):
    from agent_core.tool_catalog import build_tools

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")
    dev_names = _tool_names(build_tools(runtime_profile="dev"))
    assert "execute_code" in dev_names

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "test")
    test_names = _tool_names(build_tools(runtime_profile="test"))
    assert "execute_code" in test_names


def test_code_execution_defaults_disabled_for_hosted_and_prod(monkeypatch):
    from agent_core.tool_catalog import build_tools

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    hosted_names = _tool_names(build_tools(runtime_profile="hosted"))
    assert "execute_code" not in hosted_names

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    prod_names = _tool_names(build_tools(runtime_profile="prod"))
    assert "execute_code" not in prod_names


def test_code_execution_can_be_explicitly_enabled_for_prod():
    from agent_core.tool_catalog import build_tools

    names = _tool_names(build_tools(enabled_toolsets=["code_execution"], runtime_profile="prod"))
    assert names == ["execute_code"]


def test_code_execution_description_reflects_current_enabled_tools():
    from agent_core.tool_catalog import build_tools

    tool = build_tools(enabled_toolsets=["file_read", "web", "code_execution"], runtime_profile="dev")[-1]

    assert tool.name == "execute_code"
    assert "Available sandbox tools: read_file, search_files." in tool.description
    assert "include_web=True" in tool.description
    assert "write_file" not in tool.description
    assert "patch" not in tool.description


def test_code_execution_explicit_toolset_does_not_grant_parent_terminal_or_write_tools(monkeypatch):
    from agent_core.tool_catalog import build_tools

    tool = build_tools(enabled_toolsets=["code_execution"], runtime_profile="dev")[0]
    seen = {}

    def fake_execute_code_impl(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status="success", artifact={"ok": True, "data": {}}, content="ok")

    monkeypatch.setattr("agent_core.tool_catalog.execute_code_impl", fake_execute_code_impl)

    tool.func(code='print("x")', runtime=SimpleNamespace(tool_call_id="call", config={"configurable": {"thread_id": "thread"}}))

    assert seen["enabled_tools"] == []
