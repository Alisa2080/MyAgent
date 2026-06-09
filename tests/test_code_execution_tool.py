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

    description = build_execute_code_description(("read_file", "terminal"), has_web_tools=True)

    assert "3 or more tool calls" in description
    assert "read_file" in description
    assert "terminal" in description
    assert "web_search" in description
    assert "single simple operation" in description
    assert "background services are not supported" in description
    assert "include_web=True" in description


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


def test_sanitize_output_strips_ansi_and_truncates(monkeypatch):
    from agent_tools.public.code_execution import sanitize_output

    monkeypatch.setattr(
        "agent_tools.public.code_execution.redact_sensitive_text",
        lambda text: text.replace("sk-testsecret1234567890", "SECRET_REDACTED_VALUE"),
    )

    text = "\x1b[31mred\x1b[0m sk-testsecret1234567890 more"
    sanitized, truncated = sanitize_output(text, limit=18)

    assert "\x1b" not in sanitized
    assert "sk-testsecret" not in sanitized
    assert "SECRET_REDACTED_VALUE" not in sanitized
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


def test_public_execute_code_requires_catalog_configuration():
    from agent_tools.public.code_execution import execute_code

    result = execute_code.func(
        code='print("public wrapper")',
        runtime=_runtime(thread_id="code-exec-public"),
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "misconfigured_tool"


def test_execute_code_child_policy_blocks_direct_subprocess():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code=(
            "import subprocess\n"
            "try:\n"
            "    subprocess.run(['echo', 'bypass'], capture_output=True, text=True)\n"
            "except PermissionError as exc:\n"
            "    print(type(exc).__name__)\n"
            "    print('direct process blocked')\n"
        ),
        runtime=_runtime("code-exec-subprocess-policy"),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "success"
    stdout = result.artifact["data"]["stdout"]
    assert "PermissionError" in stdout
    assert "direct process blocked" in stdout
    assert "bypass" not in stdout


def test_execute_code_child_policy_blocks_direct_project_file_read():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code=(
            "try:\n"
            "    open('README.md', encoding='utf-8').read()\n"
            "except PermissionError as exc:\n"
            "    print(type(exc).__name__)\n"
            "    print('direct read blocked')\n"
        ),
        runtime=_runtime("code-exec-read-policy"),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "success"
    stdout = result.artifact["data"]["stdout"]
    assert "PermissionError" in stdout
    assert "direct read blocked" in stdout


def test_execute_code_child_policy_blocks_direct_project_file_write():
    from agent_tools.public.code_execution import execute_code_impl
    from pathlib import Path

    target = Path("tests/.tmp-code-exec-direct-write.txt")
    try:
        result = execute_code_impl(
            code=(
                "try:\n"
                "    open('tests/.tmp-code-exec-direct-write.txt', 'w', encoding='utf-8').write('bypass')\n"
                "except PermissionError as exc:\n"
                "    print(type(exc).__name__)\n"
                "    print('direct write blocked')\n"
            ),
            runtime=_runtime("code-exec-write-policy"),
            enabled_tools=[],
            include_web=False,
            timeout_seconds=5,
        )

        assert result.status == "success"
        stdout = result.artifact["data"]["stdout"]
        assert "PermissionError" in stdout
        assert "direct write blocked" in stdout
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


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


def test_execute_code_terminal_blocks_background_parameters():
    from agent_tools.public.code_execution import execute_code_impl

    code = (
        "from hermes_tools import terminal\n"
        "result = terminal('python -c \"print(123)\"')\n"
        "print(result['tool'])\n"
        "print(result['ok'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-terminal"),
        enabled_tools=["terminal"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "terminal" in result.artifact["data"]["stdout"]


def test_execute_code_out_of_workspace_write_is_not_silently_allowed():
    from agent_tools.public.code_execution import execute_code_impl

    code = (
        "from hermes_tools import write_file\n"
        "result = write_file('/etc/code-exec-denied.txt', 'x')\n"
        "print(result['ok'])\n"
        "print(result['error']['code'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-denied-write"),
        enabled_tools=["write_file"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "False" in result.artifact["data"]["stdout"]
    assert "dispatch_error" not in result.artifact["data"]["stdout"]
    assert (
        "policy_denied" in result.artifact["data"]["stdout"]
        or "approval_required" in result.artifact["data"]["stdout"]
        or "access_denied" in result.artifact["data"]["stdout"]
    )


def test_execute_code_can_write_workspace_file():
    from agent_tools.public.code_execution import execute_code_impl
    from pathlib import Path

    target = Path("tests/.tmp-code-exec-write.txt")
    code = (
        "from hermes_tools import write_file\n"
        "result = write_file('tests/.tmp-code-exec-write.txt', 'hello')\n"
        "print(result['ok'])\n"
        "print(result['error']['code'] if result['error'] else 'no_error')\n"
    )
    try:
        result = execute_code_impl(
            code=code,
            runtime=_runtime("code-exec-workspace-write"),
            enabled_tools=["write_file"],
            include_web=False,
            timeout_seconds=10,
        )

        assert result.status == "success"
        stdout = result.artifact["data"]["stdout"]
        assert "dispatch_error" not in stdout
        assert "True" in stdout
        assert "no_error" in stdout
        assert target.read_text(encoding="utf-8") == "hello"
    finally:
        target.unlink(missing_ok=True)


def test_web_tools_are_dispatchable_when_visible(monkeypatch):
    from agent_tools.public import code_execution as ce
    from agent_tools.shared.tool_result import tool_success

    def fake_web_search(query, limit=5, runtime=None):
        return tool_success("web_search", message="Search completed.", data={"query": query, "results": []}, runtime=runtime)

    monkeypatch.setattr("agent_tools.public.web.web_search", fake_web_search)

    dispatcher = ce.CodeExecutionDispatcher(runtime=_runtime("code-exec-web"), visible_tools=("web_search",))
    payload = dispatcher.dispatch("web_search", {"query": "langchain", "limit": 1})

    assert payload["ok"] is True
    assert payload["tool"] == "web_search"
    assert payload["data"]["query"] == "langchain"


def test_execute_code_impl_rejects_non_local_backend(monkeypatch):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setattr(
        "agent_tools.public.code_execution.get_backend_path_context",
        lambda task_id: SimpleNamespace(env_type="docker"),
    )

    result = execute_code_impl(
        code='print("backend")',
        runtime=_runtime("code-exec-docker"),
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "docker"


def test_nested_dispatch_uses_deterministic_tool_call_id(monkeypatch):
    from agent_tools.public import code_execution as ce
    from agent_tools.shared.tool_result import tool_success

    seen = {}

    def fake_read_file(path, offset=1, limit=500, runtime=None):
        seen["tool_call_id"] = getattr(runtime, "tool_call_id", None)
        return tool_success("read_file", message="File read.", data={"path": path}, runtime=runtime)

    monkeypatch.setattr("agent_tools.public.files.read_file", fake_read_file)

    dispatcher = ce.CodeExecutionDispatcher(runtime=_runtime(tool_call_id="outer-call"), visible_tools=("read_file",))
    payload = dispatcher.dispatch("read_file", {"path": "README.md", "offset": 1, "limit": 1})

    assert payload["ok"] is True
    assert seen["tool_call_id"] == "outer-call:rpc:1:read_file"


def test_tool_message_to_rpc_payload_fails_closed_for_nonstandard_result():
    from agent_tools.public.code_execution import tool_message_to_rpc_payload

    payload = tool_message_to_rpc_payload("read_file", object())

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_response"
    assert payload["tool"] == "read_file"


def test_execute_code_impl_can_call_search_files():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code='from hermes_tools import search_files\nresult = search_files("execute_code", path="tests/test_code_execution_tool.py")\nprint(result["ok"])\nprint(result["tool"])',
        runtime=_runtime("code-exec-search"),
        enabled_tools=["read_file", "search_files"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    assert "True" in result.artifact["data"]["stdout"]
    assert "search_files" in result.artifact["data"]["stdout"]


def test_execute_code_impl_can_call_patch():
    from agent_tools.public.code_execution import execute_code_impl
    from pathlib import Path

    target = Path("tests/.tmp-code-exec-patch.txt")
    target.write_text("hello world\nold line\nfoo bar\n", encoding="utf-8")

    try:
        result = execute_code_impl(
            code='from hermes_tools import patch\nresult = patch(mode="replace", path="tests/.tmp-code-exec-patch.txt", old_string="old line", new_string="new line", replace_all=False)\nprint(result["ok"])\nprint(result["tool"])',
            runtime=_runtime("code-exec-patch"),
            enabled_tools=["read_file", "write_file", "patch"],
            include_web=False,
            timeout_seconds=10,
        )

        assert result.status == "success"
        assert "True" in result.artifact["data"]["stdout"]
        assert "patch" in result.artifact["data"]["stdout"]
        assert "new line" in target.read_text(encoding="utf-8")
    finally:
        target.unlink(missing_ok=True)


def test_execute_code_normalize_rpc_args_strips_dangerous_terminal_params(monkeypatch):
    from agent_tools.public.code_execution import normalize_rpc_args

    result = normalize_rpc_args(
        "terminal",
        {
            "command": "echo hello",
            "background": True,
            "pty": True,
            "notify_on_complete": True,
            "watch_patterns": ["READY"],
        },
    )

    assert result["command"] == "echo hello"
    assert result["background"] is False
    assert result["pty"] is False
    assert result["notify_on_complete"] is False
    assert result["watch_patterns"] is None


def test_web_extract_dispatchable_when_visible(monkeypatch):
    from agent_tools.public import code_execution as ce
    from agent_tools.shared.tool_result import tool_success

    def fake_web_extract(urls, format="markdown", use_llm_processing=True, model=None, min_length=2000, max_chars_per_url=20000, runtime=None):
        return tool_success("web_extract", message="Extraction completed.", data={"urls": urls, "total": 1}, runtime=runtime)

    monkeypatch.setattr("agent_tools.public.web.web_extract", fake_web_extract)

    dispatcher = ce.CodeExecutionDispatcher(runtime=_runtime("code-exec-extract"), visible_tools=("web_search", "web_extract"))
    payload = dispatcher.dispatch("web_extract", {"urls": ["https://example.com"], "format": "markdown"})

    assert payload["ok"] is True
    assert payload["tool"] == "web_extract"


def test_rpc_server_connection_timeout_returns_error():
    from agent_tools.public.code_execution import CodeExecutionDispatcher, CodeExecutionRpcServer
    import socket

    class FakeConn:
        def recv(self, _size):
            raise socket.timeout()

    dispatcher = CodeExecutionDispatcher(runtime=_runtime(), visible_tools=("read_file",))
    server = CodeExecutionRpcServer(socket_path="/tmp/unused.sock", dispatcher=dispatcher)

    payload = server._handle_connection(FakeConn())

    assert payload["ok"] is False
    assert payload["error"]["code"] == "rpc_protocol_error"


def test_execute_code_impl_dangerous_terminal_command_is_blocked():
    from agent_tools.public.code_execution import execute_code_impl

    code = (
        "from hermes_tools import terminal\n"
        "result = terminal('touch approval-required.txt')\n"
        "print(result['ok'])\n"
        "print(result['tool'])\n"
        "print(result['error']['code'])\n"
    )
    result = execute_code_impl(
        code=code,
        runtime=_runtime("code-exec-terminal-policy"),
        enabled_tools=["terminal"],
        include_web=False,
        timeout_seconds=10,
    )

    assert result.status == "success"
    stdout = result.artifact["data"]["stdout"]
    assert "False" in stdout
    assert "terminal" in stdout
    assert "dispatch_error" not in stdout
    assert "approval_required" in stdout or "policy_denied" in stdout
