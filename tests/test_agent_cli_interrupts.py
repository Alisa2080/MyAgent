from agent_cli.interrupts import (
    extract_interrupt_requests,
    extract_interrupt_review_requests,
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


def test_extract_interrupt_review_requests_preserves_review_config_pairing():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "rm build"}},
                        {"name": "write_file", "args": {"path": "x.txt", "content": "hi"}},
                    ],
                    "review_configs": [
                        {"description": "Shell command requires review."},
                        {"description": "File write requires review."},
                    ],
                }
            }
        ]
    }

    requests = extract_interrupt_review_requests(result)

    assert [item.action_request["name"] for item in requests] == ["terminal", "write_file"]
    assert requests[0].review_config["description"] == "Shell command requires review."
    assert requests[1].review_config["description"] == "File write requires review."


def test_extract_interrupt_review_requests_handles_missing_review_configs():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "pwd"}},
                    ],
                }
            }
        ]
    }

    requests = extract_interrupt_review_requests(result)

    assert len(requests) == 1
    assert requests[0].action_request["name"] == "terminal"
    assert requests[0].review_config == {}


from agent_cli.approval import ApprovalRequest, collect_approval_decisions, summarize_args


def test_summarize_args_prefers_command_and_path():
    assert summarize_args({"command": "pytest tests", "cwd": "/repo"}) == 'command="pytest tests"'
    assert summarize_args({"path": "README.md", "content": "x" * 200}) == 'path="README.md"'


def test_collect_approval_decisions_supports_approve_reject_respond_edit():
    requests = [
        ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {"description": "review"}),
        ApprovalRequest({"name": "memory_manage", "args": {"action": "set"}}, {}),
        ApprovalRequest({"name": "skill_manage", "args": {"name": "old"}}, {}),
        ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {}),
    ]
    answers = iter([
        "y",
        "n", "not useful",
        "r", "please explain first",
        "e", '{"name": "new"}',
    ])

    resume = collect_approval_decisions(requests, input_func=lambda prompt: next(answers), print_func=lambda text="": None)

    assert resume == {
        "decisions": [
            {"type": "approve"},
            {"type": "reject", "message": "not useful"},
            {"type": "respond", "message": "please explain first"},
            {
                "type": "edit",
                "edited_action": {"name": "write_file", "args": {"name": "new"}},
            },
        ]
    }


def test_collect_approval_decisions_supports_approve_all_and_reject_all():
    requests = [
        ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {}),
        ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {}),
    ]

    approved = collect_approval_decisions(
        requests,
        input_func=lambda prompt: "a",
        print_func=lambda text="": None,
    )
    assert approved == {"decisions": [{"type": "approve"}, {"type": "approve"}]}

    answers = iter(["q", "stop now"])
    rejected = collect_approval_decisions(
        requests,
        input_func=lambda prompt: next(answers),
        print_func=lambda text="": None,
    )
    assert rejected == {
        "decisions": [
            {"type": "reject", "message": "stop now"},
            {"type": "reject", "message": "stop now"},
        ]
    }


def test_collect_approval_decisions_retries_invalid_edit_json():
    requests = [ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {})]
    answers = iter(["e", "{bad", "e", '{"path": "b.txt"}'])
    printed = []

    resume = collect_approval_decisions(
        requests,
        input_func=lambda prompt: next(answers),
        print_func=printed.append,
    )

    assert resume == {
        "decisions": [
            {
                "type": "edit",
                "edited_action": {"name": "write_file", "args": {"path": "b.txt"}},
            }
        ]
    }
    assert any("Invalid JSON" in line for line in printed)


def test_collect_approval_decisions_rejects_remaining_on_secondary_prompt_eof():
    requests = [
        ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {}),
        ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {}),
    ]
    answers = iter(["n"])

    def input_func(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError

    resume = collect_approval_decisions(
        requests,
        input_func=input_func,
        print_func=lambda text="": None,
    )

    assert resume == {
        "decisions": [
            {
                "type": "reject",
                "message": "Rejected because approval input ended.",
            },
            {
                "type": "reject",
                "message": "Rejected because approval input ended.",
            },
        ]
    }
