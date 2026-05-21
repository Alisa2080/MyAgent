import json
from types import SimpleNamespace

import pytest


def _runtime(thread_id: str = "file-policy-thread", tool_call_id: str | None = None):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


@pytest.fixture(autouse=True)
def clear_approval_state():
    from agent_core.permissions.approvals import clear_approvals

    clear_approvals()
    yield
    clear_approvals()


def test_workspace_write_does_not_need_approval(monkeypatch):
    import agent_tools.public.files as files

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "bytes_written": 5})

    monkeypatch.setattr(files, "write_file_tool", fake_write_file_tool)

    raw = files._write_file_impl("notes.txt", "hello", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["path"] == "notes.txt"


def test_workspace_escape_without_approval_is_denied(monkeypatch, tmp_path):
    import agent_tools.public.files as files
    from agent_core.permissions.models import PolicyDecision

    calls = []
    target = str(tmp_path / "escape.txt")

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": target, "bytes_written": 5})

    monkeypatch.setattr(files, "write_file_tool", fake_write_file_tool)
    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *_args, **_kwargs: PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": target},
        ),
    )

    raw = files._write_file_impl(target, "hello", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_workspace_escape_with_approval_passes_approved_roots(monkeypatch, tmp_path):
    import agent_tools.public.files as files
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        make_args_digest,
        record_approval,
    )
    from agent_core.permissions.models import PolicyDecision
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    target = str(tmp_path / "escape.txt")
    task_id = hermes_task_id_from_thread_id("file-policy-thread")

    record_approval(
        ApprovalRecord(
            approval_id="approval-escape",
            decision_id="decision-escape",
            task_id=task_id,
            tool_call_id="call-escape",
            tool_name="write_file",
            args_digest=make_args_digest({"path": target, "content": "hello"}),
            risk_tags=("writes_outside_workspace",),
        )
    )

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": target, "bytes_written": 5})

    monkeypatch.setattr(files, "write_file_tool", fake_write_file_tool)
    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *_args, **_kwargs: PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": target},
        ),
    )
    monkeypatch.setattr(
        files.file_policy,
        "approved_write_root_for_path",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    raw = files._write_file_impl(
        target,
        "hello",
        runtime=_runtime(tool_call_id="call-escape"),
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["approved_write_roots"] == [str(tmp_path)]

    second_raw = files._write_file_impl(
        target,
        "hello",
        runtime=_runtime(tool_call_id="call-escape"),
    )
    second_payload = json.loads(second_raw)

    assert second_payload["ok"] is False
    assert second_payload["error"]["code"] == "approval_required"
    assert len(calls) == 1


def test_sensitive_path_is_denied_without_consuming_approval(monkeypatch):
    import agent_tools.public.files as files

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "/root/.ssh/id_rsa", "bytes_written": 6})

    monkeypatch.setattr(files, "write_file_tool", fake_write_file_tool)

    raw = files._write_file_impl("/root/.ssh/id_rsa", "secret", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert calls == []


def test_patch_workspace_escape_without_approval_is_denied(monkeypatch, tmp_path):
    import agent_tools.public.files as files
    from agent_core.permissions.models import PolicyDecision

    calls = []
    target = str(tmp_path / "escape.txt")

    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *_args, **_kwargs: PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": target},
        ),
    )
    monkeypatch.setattr(
        files,
        "patch_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"status": "success"}),
    )

    raw = files._patch_impl(
        mode="replace",
        path=target,
        old_string="old",
        new_string="new",
        runtime=_runtime(),
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_patch_workspace_escape_with_approval_passes_approved_roots(monkeypatch, tmp_path):
    import agent_tools.public.files as files
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        make_args_digest,
        record_approval,
    )
    from agent_core.permissions.models import PolicyDecision
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    target = str(tmp_path / "escape.txt")
    task_id = hermes_task_id_from_thread_id("file-policy-thread")
    args = canonical_tool_args(
        "patch",
        {
            "path": target,
            "old_string": "old",
            "new_string": "new",
        },
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-patch",
            decision_id="decision-patch",
            task_id=task_id,
            tool_call_id="call-patch",
            tool_name="patch",
            args_digest=make_args_digest(args),
            risk_tags=("writes_outside_workspace",),
        )
    )
    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *_args, **_kwargs: PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": target},
        ),
    )
    monkeypatch.setattr(
        files.file_policy,
        "approved_write_root_for_path",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        files,
        "patch_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"status": "success"}),
    )

    raw = files._patch_impl(
        mode="replace",
        path=target,
        old_string="old",
        new_string="new",
        runtime=_runtime(tool_call_id="call-patch"),
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["approved_write_roots"] == [str(tmp_path)]

    second_raw = files._patch_impl(
        mode="replace",
        path=target,
        old_string="old",
        new_string="new",
        runtime=_runtime(tool_call_id="call-patch"),
    )
    second_payload = json.loads(second_raw)

    assert second_payload["ok"] is False
    assert second_payload["error"]["code"] == "approval_required"
    assert len(calls) == 1
