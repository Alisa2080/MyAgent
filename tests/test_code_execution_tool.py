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


def _runtime(thread_id="code-exec-thread", tool_call_id="call-execute"):
    return SimpleNamespace(
        config={"configurable": {"thread_id": thread_id}},
        tool_call_id=tool_call_id,
    )


def test_tool_message_artifact_is_normalized_to_script_dict():
    from agent_tools.public.code_execution import tool_message_to_rpc_payload
    from agent_tools.shared.tool_result import tool_success

    message = tool_success(
        "read_file",
        message="File read.",
        data={"path": "README.md"},
        meta={"truncated": False},
        runtime=_runtime(),
    )

    payload = tool_message_to_rpc_payload("read_file", message)

    assert payload == {
        "ok": True,
        "tool": "read_file",
        "message": "File read.",
        "data": {"path": "README.md"},
        "error": None,
        "meta": {"truncated": False},
    }


def test_rpc_dispatch_rejects_unavailable_tool():
    from agent_tools.public.code_execution import CodeExecutionDispatcher

    dispatcher = CodeExecutionDispatcher(runtime=_runtime(), visible_tools=("read_file",))
    payload = dispatcher.dispatch("terminal", {"command": "pwd"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_not_available"


def test_rpc_dispatch_enforces_tool_call_limit():
    from agent_tools.public.code_execution import CodeExecutionDispatcher

    dispatcher = CodeExecutionDispatcher(runtime=_runtime(), visible_tools=("read_file",), max_tool_calls=1)
    dispatcher.dispatch("read_file", {"path": "README.md", "offset": 1, "limit": 1})
    payload = dispatcher.dispatch("read_file", {"path": "README.md", "offset": 1, "limit": 1})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_call_limit_exceeded"


def test_execute_code_impl_returns_stdout_for_simple_script():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='print("hello from code")',
        runtime=_runtime(),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "success"
    assert result.artifact["ok"] is True
    assert result.artifact["data"]["stdout"] == "hello from code\n"
    assert result.artifact["data"]["returncode"] == 0


def test_execute_code_impl_reports_nonzero_exit():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='import sys\nprint("bad")\nsys.exit(7)',
        runtime=_runtime(),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "child_failed"
    assert result.artifact["data"]["returncode"] == 7


def test_execute_code_impl_can_call_read_file():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='from hermes_tools import read_file\nresult = read_file("README.md", limit=1)\nprint(result["ok"])\nprint(result["tool"])',
        runtime=_runtime(),
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "True" in result.artifact["data"]["stdout"]
    assert "read_file" in result.artifact["data"]["stdout"]


def test_public_execute_code_uses_local_executor():
    from agent_tools.public.code_execution import execute_code

    result = execute_code.func(
        code='print("public wrapper")',
        runtime=_runtime(thread_id="code-exec-public"),
    )

    assert result.status == "success"
    assert "public wrapper" in result.artifact["data"]["stdout"]


def test_visible_tools_include_web_only_when_requested():
    from agent_tools.public.code_execution import resolve_visible_tools_for_profile

    assert "web_search" not in resolve_visible_tools_for_profile(
        enabled_tools=["read_file", "web_search"],
        runtime_profile="dev",
        include_web=False,
    )
    assert "web_search" in resolve_visible_tools_for_profile(
        enabled_tools=["read_file", "web_search"],
        runtime_profile="dev",
        include_web=True,
    )
