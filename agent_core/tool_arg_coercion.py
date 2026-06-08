from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolArgCoercionError(ValueError):
    tool_name: str
    field_errors: list[str]

    def __str__(self) -> str:
        joined = "; ".join(self.field_errors) if self.field_errors else "invalid arguments"
        return f"Invalid input for {self.tool_name}: {joined}"


def normalize_tool_args(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return args

    tool_name = str(getattr(tool, "name", getattr(tool, "__name__", "")) or "")
    schema = _tool_arg_schema(tool)
    if not schema:
        return dict(args)

    coerced = dict(args)
    for key, value in args.items():
        field_schema = schema.get(key)
        if not isinstance(field_schema, dict):
            continue
        coerced[key] = _coerce_value(value, field_schema)

    return _validate_with_pydantic(tool, coerced, tool_name)


def _tool_arg_schema(tool: Any) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    model_json_schema = getattr(args_schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            properties = (model_json_schema() or {}).get("properties")
            if isinstance(properties, dict):
                return properties
        except Exception:
            pass

    model_fields = getattr(args_schema, "model_fields", None)
    if isinstance(model_fields, dict):
        schema: dict[str, Any] = {}
        for name, field in model_fields.items():
            annotation = getattr(field, "annotation", None)
            schema[name] = _schema_from_annotation(annotation)
        return {key: value for key, value in schema.items() if value}

    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return args
    return {}


def _schema_from_annotation(annotation: Any) -> dict[str, Any]:
    origin = getattr(annotation, "__origin__", None)
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is list or origin is list:
        return {"type": "array"}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _validate_with_pydantic(tool: Any, args: dict[str, Any], tool_name: str) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    model_validate = getattr(args_schema, "model_validate", None)
    if not callable(model_validate):
        return args
    try:
        model = model_validate(args)
    except Exception as exc:
        errors = _format_validation_errors(exc)
        raise ToolArgCoercionError(tool_name=tool_name, field_errors=errors) from exc
    model_dump = getattr(model, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else args
    if isinstance(model, dict):
        return model
    return args


def _format_validation_errors(exc: Exception) -> list[str]:
    errors_method = getattr(exc, "errors", None)
    if not callable(errors_method):
        return [str(exc)]
    formatted = []
    for item in errors_method():
        loc = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "invalid value"))
        formatted.append(f"{loc}: {message}" if loc else message)
    return formatted or [str(exc)]


def _coerce_value(value: Any, schema: dict[str, Any]) -> Any:
    expected = _expected_type(schema)
    if _schema_allows_null(schema) and isinstance(value, str) and value.strip().lower() == "null":
        return None
    if expected == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            parsed = _coerce_json(value, list)
            if isinstance(parsed, list):
                return parsed
        return [value]
    if not isinstance(value, str):
        return value
    if expected == "integer":
        return _coerce_integer(value)
    if expected == "number":
        return _coerce_number(value)
    if expected == "boolean":
        return _coerce_boolean(value)
    if expected == "object":
        return _coerce_json(value, dict)
    return value


def _expected_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        non_null = [str(item) for item in value if item != "null"]
        return non_null[0] if len(non_null) == 1 else None
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if not isinstance(variants, list):
            continue
        expected_values = []
        for variant in variants:
            expected = _expected_type(variant)
            if expected is not None and expected != "null":
                expected_values.append(expected)
        unique = list(dict.fromkeys(expected_values))
        return unique[0] if len(unique) == 1 else None
    return None


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    value = schema.get("type")
    if value == "null":
        return True
    if isinstance(value, list) and "null" in value:
        return True
    if schema.get("nullable") is True:
        return True
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and any(
            isinstance(variant, dict) and variant.get("type") == "null"
            for variant in variants
        ):
            return True
    return False


def _coerce_integer(value: str) -> Any:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return value
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return value
    if parsed == int(parsed):
        return int(parsed)
    return value


def _coerce_number(value: str) -> Any:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return value
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return value
    return int(parsed) if parsed == int(parsed) else parsed


def _coerce_boolean(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _coerce_json(value: str, expected_python_type: type) -> Any:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, expected_python_type) else value
