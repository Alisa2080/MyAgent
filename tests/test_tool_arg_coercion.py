from __future__ import annotations

from types import SimpleNamespace


def test_normalize_tool_args_coerces_json_schema_string_values():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(
        name="fake_tool",
        args={
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "items": {"type": "array"},
            "meta": {"type": "object"},
        },
    )
    original = {
        "count": "42",
        "ratio": "3.5",
        "enabled": "true",
        "items": "[1, 2]",
        "meta": "{\"a\": 1}",
    }

    normalized = normalize_tool_args(tool, original)

    assert normalized == {
        "count": 42,
        "ratio": 3.5,
        "enabled": True,
        "items": [1, 2],
        "meta": {"a": 1},
    }
    assert original["count"] == "42"


def test_normalize_tool_args_handles_anyof_and_nullable_values():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(
        name="terminal",
        args={
            "timeout": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "watch_patterns": {"anyOf": [{"type": "array"}, {"type": "null"}]},
            "optional": {"type": ["string", "null"]},
        },
    )

    assert normalize_tool_args(
        tool,
        {"timeout": "5", "watch_patterns": "[\"done\"]", "optional": "null"},
    ) == {"timeout": 5, "watch_patterns": ["done"], "optional": None}


def test_normalize_tool_args_rejects_invalid_json_schema_values():
    import pytest

    from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args

    invalid_cases = [
        (
            {"integer_decimal": {"type": "integer"}},
            "integer_decimal",
            "3.5",
        ),
        (
            {"not_number": {"type": "number"}},
            "not_number",
            "NaN",
        ),
        (
            {"not_boolean": {"type": "boolean"}},
            "not_boolean",
            "yes",
        ),
    ]

    for schema, key, value in invalid_cases:
        tool = SimpleNamespace(name="fake_tool", args=schema)
        with pytest.raises(ToolArgCoercionError) as exc_info:
            normalize_tool_args(tool, {key: value})
        assert key in str(exc_info.value)


def test_normalize_tool_args_preserves_large_integer_precision():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(name="fake_tool", args={"count": {"type": "integer"}})

    assert normalize_tool_args(tool, {"count": "9007199254740993"}) == {
        "count": 9007199254740993
    }


def test_normalize_tool_args_rejects_invalid_json_schema_integer():
    import pytest

    from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args

    tool = SimpleNamespace(name="fake_tool", args={"count": {"type": "integer"}})

    with pytest.raises(ToolArgCoercionError) as exc_info:
        normalize_tool_args(tool, {"count": "abc"})

    assert exc_info.value.tool_name == "fake_tool"
    assert "count" in str(exc_info.value)


def test_normalize_tool_args_wraps_scalar_for_array_targets():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(name="fake_tool", args={"patterns": {"type": "array"}})

    assert normalize_tool_args(tool, {"patterns": "done"}) == {"patterns": ["done"]}


def test_normalize_tool_args_uses_pydantic_schema_validation():
    from pydantic import BaseModel

    from agent_core.tool_arg_coercion import normalize_tool_args

    class Args(BaseModel):
        count: int
        enabled: bool
        tags: list[str]

    tool = SimpleNamespace(name="fake_tool", args_schema=Args)

    assert normalize_tool_args(tool, {"count": "7", "enabled": "false", "tags": "alpha"}) == {
        "count": 7,
        "enabled": False,
        "tags": ["alpha"],
    }


def test_normalize_tool_args_raises_controlled_error_for_invalid_input():
    import pytest
    from pydantic import BaseModel

    from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args

    class Args(BaseModel):
        count: int

    tool = SimpleNamespace(name="fake_tool", args_schema=Args)

    with pytest.raises(ToolArgCoercionError) as exc_info:
        normalize_tool_args(tool, {"count": "abc"})

    assert exc_info.value.tool_name == "fake_tool"
    assert "count" in str(exc_info.value)
