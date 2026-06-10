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


def test_local_runner_keeps_direct_subprocess_blocked():
    from agent_tools.code_execution.runners import execute_code_with_backend

    result = execute_code_with_backend(
        code='import subprocess\nsubprocess.run(["echo", "bypass"])\n',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        backend_env_type="local",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "child_failed"
    assert "policy denied" in result.artifact["data"]["stderr"]


def test_execute_code_with_backend_rejects_unknown_non_local_backend():
    from agent_tools.code_execution.runners import execute_code_with_backend

    result = execute_code_with_backend(
        code='print("hello")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        backend_env_type="ssh",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "ssh"


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
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.public.code_execution import safe_child_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("MY_TOKEN", "secret")
    monkeypatch.setenv("LANG", "C.UTF-8")

    config = CodeExecutionConfig()
    env = safe_child_env(config, {"CODE_EXECUTION_RPC_SOCKET": "/tmp/rpc.sock"})

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


def test_execute_code_project_mode_allows_direct_project_file_read():
    from agent_tools.public.code_execution import execute_code_impl

    result = execute_code_impl(
        code=(
            "content = open('README.md', encoding='utf-8').read(64)\n"
            "print(bool(content))\n"
        ),
        runtime=_runtime("code-exec-project-read-policy"),
        enabled_tools=[],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "success"
    assert "True" in result.artifact["data"]["stdout"]


def test_execute_code_strict_mode_blocks_direct_project_file_read():
    from pathlib import Path

    from agent_tools.public.code_execution import execute_code_impl

    runtime = _runtime("code-exec-strict-read-policy")
    runtime.config["configurable"]["code_execution"] = {"mode": "strict"}
    project_readme = Path("README.md").resolve()

    result = execute_code_impl(
        code=(
            "try:\n"
            f"    open({str(project_readme)!r}, encoding='utf-8').read()\n"
            "except PermissionError as exc:\n"
            "    print(type(exc).__name__)\n"
            "    print('direct read blocked')\n"
        ),
        runtime=runtime,
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


def test_execute_code_impl_rejects_unsupported_non_local_backend(monkeypatch):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setattr(
        "agent_tools.file_toolkit.backend_paths.get_backend_path_context",
        lambda task_id: SimpleNamespace(env_type="ssh", cwd="/remote/project"),
    )

    result = execute_code_impl(
        code='print("backend")',
        runtime=_runtime("code-exec-ssh"),
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "ssh"


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


def test_code_execution_config_uses_safe_fallbacks(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig

    monkeypatch.setenv("CODE_EXECUTION_MODE", "invalid")
    monkeypatch.setenv("CODE_EXECUTION_TIMEOUT_SECONDS", "-1")
    monkeypatch.setenv("CODE_EXECUTION_MAX_TOOL_CALLS", "not-an-int")

    config = CodeExecutionConfig.from_sources()

    assert config.mode == "project"
    assert config.timeout_seconds == 300
    assert config.max_tool_calls == 50
    assert any("CODE_EXECUTION_MODE" in warning for warning in config.warnings)
    assert any("CODE_EXECUTION_TIMEOUT_SECONDS" in warning for warning in config.warnings)
    assert any("CODE_EXECUTION_MAX_TOOL_CALLS" in warning for warning in config.warnings)


def test_local_success_reports_config_fallback_warnings(monkeypatch):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("CODE_EXECUTION_TIMEOUT_SECONDS", "not-a-number")

    result = execute_code_impl(
        code='print("ok")',
        runtime=_runtime(),
        enabled_tools=[],
        include_web=False,
    )

    assert result.artifact["ok"] is True
    assert result.artifact["meta"]["backend"] == "local"
    assert any(
        "CODE_EXECUTION_TIMEOUT_SECONDS" in warning
        for warning in result.artifact["meta"]["warnings"]
    )


def test_local_process_wait_honors_interrupt_flag():
    import subprocess
    import sys

    from agent_tools.code_execution.runners import _communicate_local_process
    from agent_tools.terminal_toolkit.interrupt import set_interrupt

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    set_interrupt(True)
    try:
        _, _, reason = _communicate_local_process(proc, timeout_seconds=30)
    finally:
        set_interrupt(False)
        if proc.poll() is None:
            proc.kill()

    assert reason == "interrupted"
    assert proc.poll() is not None


def test_code_execution_config_denylist_wins_over_allowlist(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig

    monkeypatch.setenv("CODE_EXECUTION_ENV_ALLOWLIST", "OPENAI_API_KEY,SAFE_FLAG")
    monkeypatch.setenv("CODE_EXECUTION_SECRET_DENYLIST", "API_KEY")

    config = CodeExecutionConfig.from_sources()

    assert "SAFE_FLAG" in config.env_allowlist
    assert "OPENAI_API_KEY" in config.env_allowlist
    assert config.env_name_allowed("SAFE_FLAG")
    assert not config.env_name_allowed("OPENAI_API_KEY")


def test_code_execution_config_explicit_allowlist_overrides_environment(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig

    monkeypatch.setenv("CODE_EXECUTION_ENV_ALLOWLIST", "FROM_ENV")

    config = CodeExecutionConfig.from_sources({"env_allowlist": ["EXPLICIT"]})
    empty_config = CodeExecutionConfig.from_sources({"env_allowlist": []})

    assert config.env_allowlist == ("EXPLICIT",)
    assert empty_config.env_allowlist == ()


def test_safe_child_env_denylist_wins_over_allowlist(monkeypatch):
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import safe_child_env

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("SAFE_FLAG", "ok")

    config = CodeExecutionConfig(
        env_allowlist=("OPENAI_API_KEY", "SAFE_FLAG"),
        secret_denylist=("API_KEY",),
    )

    env = safe_child_env(config)

    assert env["SAFE_FLAG"] == "ok"
    assert "OPENAI_API_KEY" not in env


def test_execute_code_impl_allows_environment_config_when_limits_are_omitted(monkeypatch):
    from agent_tools.public import code_execution

    monkeypatch.setenv("CODE_EXECUTION_TIMEOUT_SECONDS", "17")
    seen = {}

    def fake_execute_code_with_backend(**kwargs):
        seen["config"] = kwargs["config"]
        return object()

    monkeypatch.setattr(code_execution, "execute_code_with_backend", fake_execute_code_with_backend)

    code_execution.execute_code_impl(
        code='print("x")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
    )

    assert seen["config"].timeout_seconds == 17


def test_execute_code_impl_runtime_config_overrides_environment(monkeypatch):
    from agent_tools.public import code_execution

    monkeypatch.setenv("CODE_EXECUTION_TIMEOUT_SECONDS", "17")
    runtime = SimpleNamespace(
        config={
            "configurable": {
                "code_execution": {
                    "timeout_seconds": 23,
                    "mode": "strict",
                }
            }
        }
    )
    seen = {}

    def fake_execute_code_with_backend(**kwargs):
        seen["config"] = kwargs["config"]
        return object()

    monkeypatch.setattr(code_execution, "execute_code_with_backend", fake_execute_code_with_backend)

    code_execution.execute_code_impl(
        code='print("x")',
        runtime=runtime,
        enabled_tools=[],
        include_web=False,
    )

    assert seen["config"].timeout_seconds == 23
    assert seen["config"].mode == "strict"


def test_docker_file_rpc_workspace_requires_persistent_host_workspace():
    from types import SimpleNamespace

    from agent_tools.code_execution.runners import docker_host_workspace_dir

    persistent_env = SimpleNamespace(_persistent=True, _workspace_dir="/tmp/workspace")
    non_persistent_env = SimpleNamespace(_persistent=False, _workspace_dir="/tmp/workspace")
    missing_workspace_env = SimpleNamespace(_persistent=True, _workspace_dir=None)

    assert docker_host_workspace_dir(persistent_env) == "/tmp/workspace"
    assert docker_host_workspace_dir(non_persistent_env) is None
    assert docker_host_workspace_dir(missing_workspace_env) is None


def test_file_rpc_stub_generation_uses_rpc_directory_env():
    from agent_tools.code_execution.stubs import generate_file_rpc_tools_module

    source = generate_file_rpc_tools_module(("read_file", "terminal"))

    assert "CODE_EXECUTION_RPC_DIR" in source
    assert "CODE_EXECUTION_RPC_MAX_RESPONSE_BYTES" in source
    assert "req_" in source
    assert "res_" in source
    assert "def read_file(" in source
    assert "def terminal(" in source
    assert "def web_search(" not in source


def test_file_rpc_stub_reports_unavailable_when_rpc_env_is_missing(tmp_path, monkeypatch):
    from agent_tools.code_execution.stubs import generate_file_rpc_tools_module

    monkeypatch.chdir(tmp_path)
    namespace = {}
    exec(generate_file_rpc_tools_module(("read_file",)), namespace)

    result = namespace["read_file"]("README.md")

    assert result["ok"] is False
    assert result["error"]["code"] == "rpc_unavailable"
    assert not list(tmp_path.glob("req_*.json"))


def test_file_rpc_bridge_dispatches_request(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            return {"ok": True, "tool": tool_name, "message": "done", "data": args, "error": None, "meta": {}}

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), poll_interval_seconds=0.01)
    bridge.start()
    try:
        request_tmp = rpc_dir / ".req_abc.tmp"
        request_tmp.write_text(
            json.dumps({"id": "abc", "tool": "read_file", "args": {"path": "README.md"}}),
            encoding="utf-8",
        )
        request_tmp.replace(rpc_dir / "req_abc.json")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_abc.json").exists():
            time.sleep(0.01)
        payload = json.loads((rpc_dir / "res_abc.json").read_text(encoding="utf-8"))
    finally:
        bridge.close()

    assert payload["ok"] is True
    assert payload["tool"] == "read_file"
    assert payload["data"] == {"path": "README.md"}
    assert not bridge.is_alive()


def test_file_rpc_bridge_rejects_large_request(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            raise AssertionError("large payload should not dispatch")

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), max_request_bytes=10, poll_interval_seconds=0.01)
    bridge.start()
    try:
        request_tmp = rpc_dir / ".req_big.tmp"
        request_tmp.write_text("x" * 100, encoding="utf-8")
        request_tmp.replace(rpc_dir / "req_big.json")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_big.json").exists():
            time.sleep(0.01)
        payload = json.loads((rpc_dir / "res_big.json").read_text(encoding="utf-8"))
    finally:
        bridge.close()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "rpc_payload_too_large"


def test_file_rpc_bridge_rejects_non_object_json_without_stopping(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            return {"ok": True, "tool": tool_name, "message": "done", "data": args, "error": None, "meta": {}}

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), poll_interval_seconds=0.01)
    bridge.start()
    try:
        bad_tmp = rpc_dir / ".req_bad.tmp"
        bad_tmp.write_text("[]", encoding="utf-8")
        bad_tmp.replace(rpc_dir / "req_bad.json")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_bad.json").exists():
            time.sleep(0.01)
        bad_payload = json.loads((rpc_dir / "res_bad.json").read_text(encoding="utf-8"))

        good_tmp = rpc_dir / ".req_good.tmp"
        good_tmp.write_text(
            json.dumps({"id": "good", "tool": "read_file", "args": {"path": "README.md"}}),
            encoding="utf-8",
        )
        good_tmp.replace(rpc_dir / "req_good.json")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_good.json").exists():
            time.sleep(0.01)
        good_payload = json.loads((rpc_dir / "res_good.json").read_text(encoding="utf-8"))
    finally:
        bridge.close()

    assert bad_payload["error"]["code"] == "rpc_protocol_error"
    assert good_payload["ok"] is True
    assert not bridge.is_alive()


def test_file_rpc_bridge_replaces_oversized_response(tmp_path):
    import json
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    class Dispatcher:
        def dispatch(self, tool_name, args):
            return {
                "ok": True,
                "tool": tool_name,
                "message": "x" * 500,
                "data": {},
                "error": None,
                "meta": {},
            }

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(
        rpc_dir=rpc_dir,
        dispatcher=Dispatcher(),
        max_response_bytes=120,
        poll_interval_seconds=0.01,
    )
    bridge.start()
    try:
        request_tmp = rpc_dir / ".req_large-response.tmp"
        request_tmp.write_text(
            json.dumps({"tool": "read_file", "args": {}}),
            encoding="utf-8",
        )
        request_tmp.replace(rpc_dir / "req_large-response.json")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (rpc_dir / "res_large-response.json").exists():
            time.sleep(0.01)
        payload = json.loads(
            (rpc_dir / "res_large-response.json").read_text(encoding="utf-8")
        )
    finally:
        bridge.close()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "rpc_response_too_large"


def test_file_rpc_bridge_close_stops_poller_when_dispatch_is_blocked(tmp_path):
    import json
    import threading
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge

    release = threading.Event()
    started = threading.Event()

    class Dispatcher:
        def dispatch(self, tool_name, args):
            from agent_tools.terminal_toolkit.interrupt import is_interrupted

            started.set()
            while not release.wait(timeout=0.01):
                if is_interrupted():
                    break
            return {"ok": True, "tool": tool_name, "message": "done", "data": {}, "error": None, "meta": {}}

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), poll_interval_seconds=0.01)
    bridge.start()
    request_tmp = rpc_dir / ".req_blocked.tmp"
    request_tmp.write_text(
        json.dumps({"tool": "terminal", "args": {"command": "sleep 10"}}),
        encoding="utf-8",
    )
    request_tmp.replace(rpc_dir / "req_blocked.json")
    assert started.wait(timeout=2)

    try:
        bridge.close()
        assert not bridge.is_alive()
        assert not bridge.has_active_dispatches()
    finally:
        release.set()


def test_file_rpc_bridge_close_clears_dispatch_interrupt_flag(monkeypatch, tmp_path):
    import json
    import threading
    import time

    from agent_tools.code_execution.file_rpc import FileRpcBridge
    from agent_tools.terminal_toolkit import interrupt

    release = threading.Event()
    started = threading.Event()
    result_holder = {}
    interrupt_calls = []
    real_set_interrupt = interrupt.set_interrupt

    def recording_set_interrupt(active, thread_id=None):
        interrupt_calls.append((active, thread_id))
        real_set_interrupt(active, thread_id=thread_id)

    monkeypatch.setattr(interrupt, "set_interrupt", recording_set_interrupt)

    class Dispatcher:
        def dispatch(self, tool_name, args):
            started.set()
            while not interrupt.is_interrupted():
                time.sleep(0.01)
            result_holder["saw_interrupt"] = True
            release.set()
            return {"ok": True, "tool": tool_name, "message": "done", "data": {}, "error": None, "meta": {}}

    rpc_dir = tmp_path / "rpc"
    bridge = FileRpcBridge(rpc_dir=rpc_dir, dispatcher=Dispatcher(), poll_interval_seconds=0.01)
    bridge.start()
    request_tmp = rpc_dir / ".req_blocked.tmp"
    request_tmp.write_text(
        json.dumps({"tool": "terminal", "args": {"command": "sleep 10"}}),
        encoding="utf-8",
    )
    request_tmp.replace(rpc_dir / "req_blocked.json")
    assert started.wait(timeout=2)

    bridge.close()
    assert release.wait(timeout=2)

    assert result_holder["saw_interrupt"] is True
    interrupted_ids = {thread_id for active, thread_id in interrupt_calls if active}
    cleared_ids = {thread_id for active, thread_id in interrupt_calls if not active}
    assert interrupted_ids
    assert interrupted_ids <= cleared_ids


def test_local_rpc_rejects_non_object_json_without_stopping(tmp_path):
    import json
    import socket
    import time

    from agent_tools.code_execution.local_rpc import CodeExecutionRpcServer

    class Dispatcher:
        def dispatch(self, tool_name, args):
            return {"ok": True, "tool": tool_name, "message": "done", "data": args, "error": None, "meta": {}}

    def rpc_call(socket_path, payload):
        body = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(len(body).to_bytes(8, "big") + body)
            header = client.recv(8)
            size = int.from_bytes(header, "big")
            data = b""
            while len(data) < size:
                data += client.recv(size - len(data))
        return json.loads(data.decode("utf-8"))

    socket_path = tmp_path / "rpc.sock"
    server = CodeExecutionRpcServer(socket_path=str(socket_path), dispatcher=Dispatcher())
    server.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not socket_path.exists():
            time.sleep(0.01)
        bad_payload = rpc_call(socket_path, [])
        good_payload = rpc_call(socket_path, {"tool": "read_file", "args": {"path": "README.md"}})
    finally:
        server.close()

    assert bad_payload["error"]["code"] == "rpc_protocol_error"
    assert good_payload["ok"] is True
    assert not server.is_alive()


def test_rpc_payload_redacts_secret_before_returning_to_script():
    from agent_tools.code_execution.dispatch import tool_message_to_rpc_payload
    from agent_tools.shared.tool_result import tool_success

    value = tool_success(
        "read_file",
        message="File read.",
        data={"content": "OPENAI_API_KEY=secret-value"},
        runtime=None,
    )

    payload = tool_message_to_rpc_payload("read_file", value)

    assert "secret-value" not in str(payload)
    assert "[REDACTED]" in str(payload)


def test_dispatch_exception_payload_is_redacted(monkeypatch):
    from agent_tools.code_execution.dispatch import CodeExecutionDispatcher

    dispatcher = CodeExecutionDispatcher(runtime=_runtime("redacted-dispatch"), visible_tools=("read_file",))

    def raise_secret(*args, **kwargs):
        raise RuntimeError("OPENAI_API_KEY=secret-value")

    monkeypatch.setattr(dispatcher, "_call_tool", raise_secret)

    payload = dispatcher.dispatch("read_file", {})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "dispatch_error"
    assert "secret-value" not in str(payload)
    assert "[REDACTED]" in str(payload)


def test_terminal_rpc_default_workdir_uses_backend_context(monkeypatch):
    from agent_tools.code_execution.dispatch import CodeExecutionDispatcher

    seen = {}
    runtime = _runtime("terminal-default-workdir")

    monkeypatch.setattr(
        "agent_tools.code_execution.dispatch.get_backend_path_context",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        "agent_tools.file_toolkit.backend_paths.get_backend_path_context",
        lambda task_id: SimpleNamespace(env_type="docker", cwd="/workspace/project"),
    )

    dispatcher = CodeExecutionDispatcher(runtime=runtime, visible_tools=("terminal",))

    def fake_call(tool_name, args, *, nested_tool_call_id):
        seen.update(args)
        return {"ok": True, "tool": tool_name, "message": "done", "data": {}, "error": None, "meta": {}}

    monkeypatch.setattr(dispatcher, "_call_tool", fake_call)

    dispatcher.dispatch("terminal", {"command": "pwd"})

    assert seen["workdir"] == "/workspace/project"


def test_terminal_rpc_default_workdir_can_be_overridden_for_strict_mode(monkeypatch):
    from agent_tools.code_execution.dispatch import CodeExecutionDispatcher

    seen = {}
    dispatcher = CodeExecutionDispatcher(
        runtime=_runtime("terminal-strict-workdir"),
        visible_tools=("terminal",),
        terminal_default_workdir="/workspace/.code_execution/run-id",
    )

    def fake_call(tool_name, args, *, nested_tool_call_id):
        seen.update(args)
        return {"ok": True, "tool": tool_name, "message": "done", "data": {}, "error": None, "meta": {}}

    monkeypatch.setattr(dispatcher, "_call_tool", fake_call)

    dispatcher.dispatch("terminal", {"command": "pwd"})

    assert seen["workdir"] == "/workspace/.code_execution/run-id"


def test_docker_environment_exposes_auto_mounted_cwd_as_host_workspace(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_tools.terminal_toolkit.environments import docker as docker_module

    monkeypatch.setattr(docker_module, "_ensure_docker_available", lambda: None)
    monkeypatch.setattr(docker_module.DockerEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(docker_module, "find_docker", lambda: "docker")
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.environments.base.get_sandbox_dir",
        lambda: tmp_path / "sandboxes",
    )
    monkeypatch.setattr(
        docker_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="container-id\n", stderr="", returncode=0),
    )

    env = docker_module.DockerEnvironment(
        image="python:3.11",
        persistent_filesystem=True,
        task_id="auto-cwd",
        host_cwd=str(tmp_path),
        auto_mount_cwd=True,
    )

    assert env.host_workspace_dir == str(tmp_path)
    env._container_id = None


def test_docker_environment_exposes_explicit_workspace_host_mount(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_tools.terminal_toolkit.environments import docker as docker_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(docker_module, "_ensure_docker_available", lambda: None)
    monkeypatch.setattr(docker_module.DockerEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(docker_module, "find_docker", lambda: "docker")
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.environments.base.get_sandbox_dir",
        lambda: tmp_path / "sandboxes",
    )
    monkeypatch.setattr(
        docker_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="container-id\n", stderr="", returncode=0),
    )

    env = docker_module.DockerEnvironment(
        image="python:3.11",
        persistent_filesystem=True,
        task_id="explicit-workspace",
        volumes=[f"{workspace}:/workspace:rw"],
    )

    assert env.host_workspace_dir == str(workspace)
    env._container_id = None


def test_docker_runner_rejects_windows():
    from types import SimpleNamespace
    from unittest.mock import patch, MagicMock

    from agent_tools.code_execution.runners import DockerFileRpcRunner

    runner = DockerFileRpcRunner()

    with patch("platform.system", return_value="Windows"):
        result = runner.run(
            code='print("hello")',
            runtime=SimpleNamespace(),
            enabled_tools=[],
            include_web=False,
            config=MagicMock(timeout_seconds=30, mode="project", env_allowlist=[], stdout_limit_chars=10000, stderr_limit_chars=5000, output_limit_chars=100000, max_tool_calls=50),
            task_id="test-windows",
        )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsupported_platform"


def test_docker_runner_requires_persistent_workspace(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = False
        _workspace_dir = None

    runner = DockerFileRpcRunner()

    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kw: FakeEnv(),
    )

    result = runner.run(
        code='print("hello")',
        runtime=SimpleNamespace(),
        enabled_tools=[],
        include_web=False,
        config=MagicMock(timeout_seconds=30, mode="project", env_allowlist=[], stdout_limit_chars=10000, stderr_limit_chars=5000, output_limit_chars=100000, max_tool_calls=50),
        task_id="test-no-persist",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "docker_workspace_unavailable"
    assert "persistent" in result.artifact["message"].lower()


def test_docker_runner_uses_isolated_host_run_dir_and_minimal_env(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace/project"

        def __init__(self):
            self.exec_calls = []

        def execute(self, cmd, cwd="", timeout=60):
            self.exec_calls.append({"command": cmd, "cwd": cwd, "timeout": timeout})
            if "command -v" in cmd:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            return {"output": "hello\n", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    fake_env = FakeEnv()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kw: fake_env,
    )

    runner = DockerFileRpcRunner()

    result = runner.run(
        code='print("hello from container")',
        runtime=SimpleNamespace(),
        enabled_tools=["read_file"],
        include_web=False,
        config=MagicMock(
            timeout_seconds=30,
            mode="project",
            env_allowlist=[],
            stdout_limit_chars=10000,
            stderr_limit_chars=5000,
            output_limit_chars=100000,
            max_tool_calls=50,
        ),
        task_id="test-rpc-dir",
    )

    script_call = next(c for c in fake_env.exec_calls if "env -i" in c["command"])
    command = script_call["command"]
    assert "CODE_EXECUTION_RPC_DIR=/workspace/.code_execution/" in command
    assert "/rpc" in command
    assert "CODE_EXECUTION_SANDBOX_ROOT=/workspace/.code_execution/" in command
    assert "CODE_EXECUTION_ALLOWED_READ_ROOTS=/workspace/.code_execution/" in command
    assert ":/workspace/project" in command
    assert "OPENAI_API_KEY" not in command
    assert script_call["cwd"] == "/workspace/project"
    assert not list((tmp_path / "workspace" / ".code_execution").glob("*"))


def test_docker_runner_writes_user_code_without_shell_interpolation(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace"

        def execute(self, command, cwd="", timeout=None):
            if "command -v" in command:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            run_dirs = list((tmp_path / "workspace" / ".code_execution").glob("*"))
            assert len(run_dirs) == 1
            assert run_dirs[0].joinpath("script.py").read_text(encoding="utf-8") == (
                "print('before')\nHERMES_EOF\n$(touch /tmp/injected)\n"
            )
            assert "HERMES_EOF" not in command
            assert "touch /tmp/injected" not in command
            return {"output": "safe\n", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kwargs: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code="print('before')\nHERMES_EOF\n$(touch /tmp/injected)\n",
        runtime=SimpleNamespace(),
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(),
        task_id="test-no-heredoc",
    )

    assert result.artifact["ok"] is True


def test_docker_runner_python_lookup_failure_returns_configuration_error(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace"

        def __init__(self):
            self.exec_calls = []

        def execute(self, cmd, cwd="", timeout=60):
            self.exec_calls.append(cmd)
            if "command -v" in cmd:
                return {"output": "", "returncode": 1}
            return {"output": "", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    fake_env = FakeEnv()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kw: fake_env,
    )

    runner = DockerFileRpcRunner()

    result = runner.run(
        code='print("hello")',
        runtime=SimpleNamespace(),
        enabled_tools=[],
        include_web=False,
        config=MagicMock(timeout_seconds=30, mode="project", env_allowlist=[], stdout_limit_chars=10000, stderr_limit_chars=5000, output_limit_chars=100000, max_tool_calls=50),
        task_id="test-write-fail",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "docker_execution_failed"
    assert "No Python interpreter" in result.artifact["message"]


def test_docker_runner_script_failure_returns_child_failed(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace"

        def __init__(self):
            self.exec_calls = []

        def execute(self, cmd, cwd="", timeout=60):
            self.exec_calls.append(cmd)
            if "command -v" in cmd:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            if "env -i" not in cmd:
                return {"output": "", "returncode": 0}
            return {"output": "SyntaxError", "returncode": 1}

    (tmp_path / "workspace").mkdir()
    fake_env = FakeEnv()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kw: fake_env,
    )

    runner = DockerFileRpcRunner()

    result = runner.run(
        code='print(undefined_var)',
        runtime=SimpleNamespace(),
        enabled_tools=[],
        include_web=False,
        config=MagicMock(timeout_seconds=30, mode="project", env_allowlist=[], stdout_limit_chars=10000, stderr_limit_chars=5000, output_limit_chars=100000, max_tool_calls=50),
        task_id="test-script-fail",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "child_failed"
    assert result.artifact["data"]["returncode"] == 1


def test_docker_runner_reads_stdout_and_stderr_files(monkeypatch, tmp_path):
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace"

        def execute(self, command, cwd="", timeout=None):
            if "command -v" in command:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            if "env -i" in command:
                run_dir = next((tmp_path / "workspace" / ".code_execution").glob("*"))
                (run_dir / "stdout.txt").write_text("from stdout\n", encoding="utf-8")
                (run_dir / "stderr.txt").write_text("from stderr\n", encoding="utf-8")
            return {"output": "", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kwargs: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code="print('split')",
        runtime=SimpleNamespace(),
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(),
        task_id="test-docker-streams",
    )

    assert result.artifact["ok"] is True
    assert result.artifact["data"]["stdout"] == "from stdout\n"
    assert result.artifact["data"]["stderr"] == "from stderr\n"


def test_sanitize_output_applies_total_limit_after_redaction():
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import sanitize_process_output

    config = CodeExecutionConfig(stdout_limit_chars=80, stderr_limit_chars=80, output_limit_chars=40)
    data = sanitize_process_output(
        stdout_raw="token sk-test1234567890 " + ("visible " * 10),
        stderr_raw="stderr text",
        returncode=0,
        config=config,
    )

    assert "sk-test1234567890" not in data["stdout"]
    assert len(data["stdout"]) + len(data["stderr"]) <= 40
    assert data["stdout_truncated"] is True


def test_strict_docker_mode_uses_run_directory(monkeypatch, tmp_path):
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    calls = []

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace/project"

        def execute(self, cmd, cwd="", timeout=None):
            calls.append({"command": cmd, "cwd": cwd, "timeout": timeout})
            if "command -v" in cmd:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            return {"output": "strict", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda task_id, timeout=None: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code='print("strict")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(mode="strict"),
        task_id="task-docker",
    )

    assert result.artifact["ok"] is True
    script_call = next(c for c in calls if "env -i" in c["command"])
    assert script_call["cwd"].startswith("/workspace/.code_execution/")
    assert "CODE_EXECUTION_ALLOWED_READ_ROOTS=" in script_call["command"]
    assert ":/workspace/project" not in script_call["command"]


def test_docker_timeout_terminates_container_process_group(monkeypatch, tmp_path):
    from agent_tools.code_execution.config import CodeExecutionConfig
    from agent_tools.code_execution.runners import DockerFileRpcRunner

    calls = []

    class FakeEnv:
        _persistent = True
        _workspace_dir = str(tmp_path / "workspace")
        cwd = "/workspace"

        def execute(self, command, cwd="", timeout=None):
            calls.append(command)
            if "command -v" in command:
                return {"output": "/usr/bin/python3\n", "returncode": 0}
            if "env -i" in command:
                return {"output": "[Command timed out after 1s]", "returncode": 124}
            return {"output": "", "returncode": 0}

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        "agent_tools.terminal_toolkit.terminal_tool.get_or_create_active_env",
        lambda **kwargs: FakeEnv(),
    )

    result = DockerFileRpcRunner().run(
        code="import time; time.sleep(30)",
        runtime=None,
        enabled_tools=[],
        include_web=False,
        config=CodeExecutionConfig(timeout_seconds=1),
        task_id="task-timeout",
    )

    assert result.artifact["error"]["code"] == "timeout"
    assert any("kill -TERM -- -" in command for command in calls)
    assert not list((tmp_path / "workspace" / ".code_execution").glob("*"))
