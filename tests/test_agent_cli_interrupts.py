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
