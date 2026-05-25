import json
from importlib import import_module
from types import SimpleNamespace

from langchain_core.messages import ToolMessage


def _terminal_module():
    return import_module("agent_tools.public.terminal")


def _runtime(*, thread_id: str, tool_call_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _artifact(result: ToolMessage) -> dict:
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact is not None
    return result.artifact


def test_low_risk_command_runs_without_approval(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "/workspace\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    result = terminal_tools._terminal_impl(
        command="pwd",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-low-risk", tool_call_id="call-low-risk"),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["command"] == "pwd"
    assert calls[0]["allow_network_once"] is False


def test_reviewed_command_without_approval_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._terminal_impl(
        command="touch approval-required.txt",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-review", tool_call_id="call-review"),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_reviewed_command_without_tool_call_id_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._terminal_impl(
        command="touch approval-required.txt",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-no-tool-call", tool_call_id=None),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_reviewed_network_command_with_approval_passes_network_once(monkeypatch):
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        clear_approvals,
        make_args_digest,
        record_approval,
    )
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    thread_id = "task8-network"
    tool_call_id = "call-network"
    approval_args = canonical_tool_args(
        "terminal",
        {"command": "curl https://example.com"},
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(approval_args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    result = terminal_tools._terminal_impl(
        command="curl https://example.com",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["allow_network_once"] is True
    assert calls[0]["command"] == "curl https://example.com"
    assert calls[0]["force"] is True


def test_terminal_uses_middleware_grant_without_consuming_approval(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.policy_tool_middleware import terminal_policy_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    thread_id = "terminal-grant-thread"
    tool_call_id = "call-terminal-grant"
    policy_args = terminal_policy_args({"command": "curl https://example.com"})

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(policy_args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0, "error": None}),
    )

    result = terminal_tools._terminal_impl(
        command="curl https://example.com",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["force"] is True
    assert calls[0]["allow_network_once"] is True


def test_reviewed_dangerous_command_with_approval_bypasses_legacy_guard(monkeypatch):
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        clear_approvals,
        make_args_digest,
        record_approval,
    )
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    thread_id = "task8-dangerous"
    tool_call_id = "call-dangerous"
    approval_args = canonical_tool_args(
        "terminal",
        {"command": "rm -rf node_modules"},
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-dangerous",
            decision_id="decision-dangerous",
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(approval_args),
            risk_tags=("destructive_command",),
        )
    )

    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0, "error": None}),
    )

    result = terminal_tools._terminal_impl(
        command="rm -rf node_modules",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )
    payload = _artifact(result)

    assert payload["ok"] is True
    assert calls[0]["force"] is True


def test_hardline_command_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._terminal_impl(
        command="rm -rf /",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-hardline", tool_call_id="call-hardline"),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert calls == []


def test_run_terminal_accepts_and_forwards_allow_network_once(monkeypatch):
    import agent_tools.hermes_terminal_toolkit.terminal as terminal_wrapper

    calls = []

    def fake_terminal_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_wrapper, "terminal_tool", fake_terminal_tool)

    raw = terminal_wrapper.run_terminal(command="pwd", allow_network_once=True)

    assert json.loads(raw)["exit_code"] == 0
    assert calls[0]["allow_network_once"] is True
