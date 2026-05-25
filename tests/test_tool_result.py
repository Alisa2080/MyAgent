import json
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import from_legacy_json, tool_failure, tool_success


def _runtime(thread_id: str = "tool-result-thread", tool_call_id: str = "call-result"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def test_tool_success_builds_standard_tool_message_artifact():
    result = tool_success(
        "terminal",
        message="Command completed.",
        data={"output": "large output\n", "exit_code": 0},
        meta={"backend": "test"},
        runtime=_runtime(),
    )

    assert isinstance(result, ToolMessage)
    assert result.content == "Command completed."
    assert result.name == "terminal"
    assert result.tool_call_id == "call-result"
    assert result.status == "success"
    assert result.artifact == {
        "ok": True,
        "tool": "terminal",
        "message": "Command completed.",
        "data": {"output": "large output\n", "exit_code": 0},
        "error": None,
        "meta": {"backend": "test"},
    }
    assert "large output" not in result.content


def test_tool_failure_builds_standard_error_artifact():
    result = tool_failure(
        "terminal",
        "Command exited with code 2.",
        code="command_failed",
        data={"output": "failed\n", "exit_code": 2},
        runtime=_runtime(tool_call_id="call-failed"),
    )

    assert result.content == "Command exited with code 2."
    assert result.name == "terminal"
    assert result.tool_call_id == "call-failed"
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"] == {
        "code": "command_failed",
        "message": "Command exited with code 2.",
    }
    assert result.artifact["data"]["exit_code"] == 2
    assert "failed" not in result.content


def test_from_legacy_json_converts_success_and_extracts_meta_keys():
    raw = json.dumps(
        {
            "status": "success",
            "success": True,
            "message": "Read file.",
            "path": "README.md",
            "content": "full file contents",
            "_warning": "truncated",
            "_hint": "use offset",
            "truncated": True,
        }
    )

    result = from_legacy_json(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
        runtime=_runtime(tool_call_id="call-read"),
        summary=lambda payload, meta: f"Read {payload.get('path', 'file')}.",
    )

    assert result.status == "success"
    assert result.content == "Read README.md."
    assert result.tool_call_id == "call-read"
    assert result.artifact["ok"] is True
    assert result.artifact["message"] == "Read file."
    assert result.artifact["data"] == {
        "path": "README.md",
        "content": "full file contents",
    }
    assert result.artifact["meta"] == {
        "warnings": ["truncated"],
        "hint": "use offset",
        "truncated": True,
    }
    assert "full file contents" not in result.content


def test_from_legacy_json_converts_error_payload():
    raw = json.dumps({"error": "missing file", "path": "missing.txt", "code": "not_found"})

    result = from_legacy_json("read_file", raw, success_message="File read.")

    assert result.status == "error"
    assert result.content == "missing file"
    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "tool_error"
    assert result.artifact["data"] == {"path": "missing.txt", "code": "not_found"}


def test_from_legacy_json_handles_invalid_json():
    result = from_legacy_json("read_file", "not-json", success_message="File read.")

    assert result.status == "error"
    assert result.content == "Tool returned invalid response."
    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "invalid_response"
    assert result.artifact["data"] == {"raw": "not-json"}
