from types import SimpleNamespace


def test_stage_one_stub_generation_exposes_file_development_tools():
    from agent_tools.public.code_execution import generate_hermes_tools_module

    source = generate_hermes_tools_module(
        ["read_file", "search_files", "write_file", "patch", "terminal"],
        include_web=False,
    )

    assert "def read_file(" in source
    assert "def search_files(" in source
    assert "def write_file(" in source
    assert "def patch(" in source
    assert "def terminal(" in source
    assert "def web_search(" not in source
    assert "def web_extract(" not in source
    assert "__all__" in source


def test_stage_three_stub_generation_can_expose_web_tools():
    from agent_tools.public.code_execution import generate_hermes_tools_module

    source = generate_hermes_tools_module(
        ["read_file", "web_search", "web_extract"],
        include_web=True,
    )

    assert "def read_file(" in source
    assert "def web_search(" in source
    assert "def web_extract(" in source


def test_visible_sandbox_tools_intersects_whitelist_and_enabled_tools():
    from agent_tools.public.code_execution import visible_sandbox_tools

    assert visible_sandbox_tools(
        enabled_tools=["read_file", "terminal", "web_search", "memory_manage"],
        include_web=False,
    ) == ("read_file", "terminal")
    assert visible_sandbox_tools(
        enabled_tools=["read_file", "terminal", "web_search", "memory_manage"],
        include_web=True,
    ) == ("read_file", "terminal", "web_search")


def test_schema_description_lists_only_visible_tools():
    from agent_tools.public.code_execution import build_execute_code_description

    description = build_execute_code_description(("read_file", "terminal"))

    assert "3 or more tool calls" in description
    assert "read_file" in description
    assert "terminal" in description
    assert "web_search" not in description
    assert "single simple operation" in description
    assert "background services are not supported" in description
