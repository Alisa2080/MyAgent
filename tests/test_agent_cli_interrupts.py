from agent_cli.interrupts import (
    build_resume_value,
    extract_interrupt_requests,
    has_interrupt,
)


def test_has_interrupt_detects_key():
    assert has_interrupt({"__interrupt__": [{"value": {"action_requests": []}}]})
    assert not has_interrupt({"messages": []})


def test_extract_interrupt_requests_reads_action_requests():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "rm file"}}
                    ]
                }
            }
        ]
    }

    requests = extract_interrupt_requests(result)

    assert requests == [{"name": "terminal", "args": {"command": "rm file"}}]


def test_extract_interrupt_requests_reads_action_requests_from_tuple_value_dict():
    result = {
        "__interrupt__": (
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "ls"}}
                    ]
                }
            },
        )
    }

    requests = extract_interrupt_requests(result)

    assert requests == [{"name": "terminal", "args": {"command": "ls"}}]


def test_extract_interrupt_requests_reads_action_requests_from_object_value():
    class FakeInterrupt:
        def __init__(self, value):
            self.value = value

    result = {
        "__interrupt__": [
            FakeInterrupt(
                {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "pwd"}}
                    ]
                }
            )
        ]
    }

    requests = extract_interrupt_requests(result)

    assert requests == [{"name": "terminal", "args": {"command": "pwd"}}]


def test_extract_interrupt_requests_handles_unknown_shape():
    assert extract_interrupt_requests({"__interrupt__": ["raw"]}) == [{"raw": "raw"}]


def test_build_resume_value_approves_all_requests():
    resume = build_resume_value(approved=True, request_count=2)

    assert resume == {
        "decisions": [
            {"type": "approve"},
            {"type": "approve"},
        ]
    }


def test_build_resume_value_rejects_all_requests():
    resume = build_resume_value(approved=False, request_count=1)

    assert resume == {
        "decisions": [
            {"type": "reject", "message": "Rejected by user."},
        ]
    }


from agent_cli.rendering import format_interrupt_summary, latest_ai_text


def test_format_interrupt_summary_includes_tool_preview():
    summary = format_interrupt_summary([
        {"name": "terminal", "args": {"command": "pytest tests"}}
    ])

    assert "Approval required" in summary
    assert "terminal" in summary
    assert "pytest tests" in summary


def test_format_interrupt_summary_handles_non_dict_request():
    summary = format_interrupt_summary(["not-dict"])

    assert "not-dict" in summary


def test_latest_ai_text_reads_last_ai_message():
    result = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }

    assert latest_ai_text(result) == "hello"


def test_latest_ai_text_reads_langchain_text_content_block():
    result = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            }
        ]
    }

    assert latest_ai_text(result) == "hello"


def test_latest_ai_text_reads_mixed_text_content_blocks():
    result = {
        "messages": [
            {
                "role": "assistant",
                "content": ["a", {"type": "text", "text": "b"}],
            }
        ]
    }

    text = latest_ai_text(result)

    assert "a" in text
    assert "b" in text
    assert "['a'" not in text
