import json
from importlib import import_module
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage


def _terminal_module():
    return import_module("agent_tools.public.terminal")


def _runtime(*, thread_id: str = "process-policy-thread", tool_call_id: str | None = "call-process"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _artifact(result: ToolMessage) -> dict:
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact is not None
    return result.artifact


@pytest.fixture(autouse=True)
def clear_approval_state():
    from agent_core.permissions.approvals import clear_approvals

    clear_approvals()
    yield
    clear_approvals()


def _owning_session(thread_id: str):
    from agent_core.session_context import hermes_task_id_from_thread_id

    return SimpleNamespace(task_id=hermes_task_id_from_thread_id(thread_id))


def test_process_poll_does_not_need_approval(monkeypatch):
    terminal_tools = _terminal_module()
    calls = []
    thread_id = "process-poll"

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return json.dumps({"session_id": "proc_1", "running": True})

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session(thread_id))
    monkeypatch.setattr(terminal_tools, "run_process", fake_run_process)

    result = terminal_tools._process_impl(
        action="poll",
        session_id="proc_1",
        runtime=_runtime(thread_id=thread_id, tool_call_id="call-poll"),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["action"] == "poll"
    assert calls[0]["session_id"] == "proc_1"


def test_process_submit_without_approval_is_denied(monkeypatch):
    terminal_tools = _terminal_module()
    calls = []
    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session("process-submit"))
    monkeypatch.setattr(terminal_tools, "run_process", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime(thread_id="process-submit", tool_call_id="call-submit"),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_process_submit_without_tool_call_id_is_denied(monkeypatch):
    terminal_tools = _terminal_module()
    calls = []
    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session("process-no-call"))
    monkeypatch.setattr(terminal_tools, "run_process", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime(thread_id="process-no-call", tool_call_id=None),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_process_submit_with_approval_runs(monkeypatch):
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        make_args_digest,
        record_approval,
    )
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    calls = []
    thread_id = "process-approved"
    tool_call_id = "call-approved"
    approval_args = canonical_tool_args(
        "process",
        {"action": "submit", "session_id": "proc_1", "data": "exit"},
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-process",
            decision_id="decision-process",
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="process",
            args_digest=make_args_digest(approval_args),
            risk_tags=("process_stdin",),
        )
    )

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return json.dumps({"session_id": "proc_1", "submitted": True})

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session(thread_id))
    monkeypatch.setattr(terminal_tools, "run_process", fake_run_process)

    result = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["action"] == "submit"
    assert calls[0]["data"] == "exit"


def test_process_uses_middleware_grant_without_consuming_approval(monkeypatch):
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.policy_tool_middleware import process_policy_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    calls = []
    thread_id = "process-grant-thread"
    tool_call_id = "call-process-grant"
    policy_args = process_policy_args({"action": "submit", "session_id": "proc_1", "data": "exit"})

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="process",
            args_digest=make_args_digest(policy_args),
            risk_tags=("process_stdin",),
        )
    )

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session(thread_id))
    monkeypatch.setattr(
        terminal_tools,
        "run_process",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"session_id": "proc_1", "submitted": True}),
    )

    result = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    payload = _artifact(result)
    assert payload["ok"] is True
    assert calls[0]["data"] == "exit"


def test_process_policy_denial_does_not_run(monkeypatch):
    from agent_core.permissions.models import PolicyDecision

    terminal_tools = _terminal_module()
    calls = []
    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session("process-denied"))
    monkeypatch.setattr(
        terminal_tools.tool_policy,
        "evaluate_tool_call",
        lambda *_args, **_kwargs: PolicyDecision.deny("blocked_process", message="Process blocked."),
    )
    monkeypatch.setattr(terminal_tools, "run_process", lambda **kwargs: calls.append(kwargs))

    result = terminal_tools._process_impl(
        action="poll",
        session_id="proc_1",
        runtime=_runtime(thread_id="process-denied", tool_call_id="call-denied"),
    )

    payload = _artifact(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert calls == []
