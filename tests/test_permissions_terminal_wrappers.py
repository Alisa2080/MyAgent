import json
from importlib import import_module
from types import SimpleNamespace


def _terminal_module():
    return import_module("agent_tools.public.terminal")


def _runtime(*, thread_id: str, tool_call_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _terminal_policy_args(
    *,
    command: str,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] | None = None,
) -> dict:
    return {
        "command": command,
        "background": background,
        "timeout": timeout,
        "workdir": workdir,
        "pty": pty,
        "notify_on_complete": notify_on_complete,
        "watch_patterns": watch_patterns,
    }


def test_low_risk_command_runs_without_approval(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "/workspace\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    raw = terminal_tools._terminal_impl(
        command="pwd",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-low-risk", tool_call_id="call-low-risk"),
    )

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert calls[0]["command"] == "pwd"
    assert calls[0]["allow_network_once"] is False


def test_reviewed_command_without_approval_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    raw = terminal_tools._terminal_impl(
        command="touch approval-required.txt",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-review", tool_call_id="call-review"),
    )

    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_reviewed_command_without_tool_call_id_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    raw = terminal_tools._terminal_impl(
        command="touch approval-required.txt",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-no-tool-call", tool_call_id=None),
    )

    payload = json.loads(raw)
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
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    thread_id = "task8-network"
    tool_call_id = "call-network"
    policy_args = _terminal_policy_args(
        command="curl https://example.com",
        background=False,
        timeout=7,
        workdir="/tmp",
        pty=True,
        notify_on_complete=True,
        watch_patterns=["ready"],
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(policy_args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    raw = terminal_tools._terminal_impl(
        command=policy_args["command"],
        background=policy_args["background"],
        timeout=policy_args["timeout"],
        workdir=policy_args["workdir"],
        pty=policy_args["pty"],
        notify_on_complete=policy_args["notify_on_complete"],
        watch_patterns=policy_args["watch_patterns"],
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert calls[0]["allow_network_once"] is True
    assert calls[0]["command"] == policy_args["command"]


def test_hardline_command_is_denied(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    monkeypatch.setattr(terminal_tools, "run_terminal", lambda **kwargs: calls.append(kwargs))

    raw = terminal_tools._terminal_impl(
        command="rm -rf /",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=_runtime(thread_id="task8-hardline", tool_call_id="call-hardline"),
    )

    payload = json.loads(raw)
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
