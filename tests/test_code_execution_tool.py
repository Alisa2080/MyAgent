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


def test_safe_child_env_removes_secret_like_variables(monkeypatch):
    from agent_tools.public.code_execution import safe_child_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("MY_TOKEN", "secret")
    monkeypatch.setenv("LANG", "C.UTF-8")

    env = safe_child_env({"CODE_EXECUTION_RPC_SOCKET": "/tmp/rpc.sock"})

    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["CODE_EXECUTION_RPC_SOCKET"] == "/tmp/rpc.sock"
    assert "OPENAI_API_KEY" not in env
    assert "MY_TOKEN" not in env


def test_sanitize_output_strips_ansi_redacts_and_truncates():
    from agent_tools.public.code_execution import sanitize_output

    text = "\x1b[31mred\x1b[0m sk-testsecret1234567890 more"
    sanitized, truncated = sanitize_output(text, limit=18)

    assert "\x1b" not in sanitized
    assert "sk-testsecret" not in sanitized
    assert len(sanitized) <= 18 + len("\n[truncated]")
    assert truncated is True


def test_terminal_args_are_forced_foreground():
    from agent_tools.public.code_execution import normalize_rpc_args

    args = normalize_rpc_args(
        "terminal",
        {
            "command": "pytest",
            "background": True,
            "pty": True,
            "notify_on_complete": True,
            "watch_patterns": ["ready"],
        },
    )

    assert args == {
        "command": "pytest",
        "background": False,
        "timeout": None,
        "workdir": None,
        "pty": False,
        "notify_on_complete": False,
        "watch_patterns": None,
    }
