from __future__ import annotations

import json
import re
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

    _validate_with_json_schema(schema, coerced, tool_name)
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


def _validate_with_json_schema(
    schema: dict[str, Any],
    args: dict[str, Any],
    tool_name: str,
) -> None:
    field_errors = []
    for key, value in args.items():
        field_schema = schema.get(key)
        if not isinstance(field_schema, dict):
            continue
        if not _value_matches_schema(value, field_schema):
            expected = _schema_description(field_schema)
            field_errors.append(
                f"{key}: expected {expected}, got {type(value).__name__}"
            )
    if field_errors:
        raise ToolArgCoercionError(tool_name=tool_name, field_errors=field_errors)


def _value_matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    if value is None:
        return _schema_allows_null(schema)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return any(
            item != "null" and _value_matches_schema(value, {"type": item})
            for item in schema_type
        )
    if isinstance(schema_type, str):
        return _value_matches_type(value, schema_type)

    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if not isinstance(variants, list):
            continue
        return any(
            isinstance(variant, dict) and _value_matches_schema(value, variant)
            for variant in variants
        )

    return True


def _value_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == value
            and value not in (float("inf"), float("-inf"))
        )
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "null":
        return value is None
    return True


def _schema_description(schema: dict[str, Any]) -> str:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if isinstance(schema_type, list):
        return " or ".join(str(item) for item in schema_type)
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list):
            parts = [
                _schema_description(variant)
                for variant in variants
                if isinstance(variant, dict)
            ]
            if parts:
                return " or ".join(parts)
    return "valid value"


def _coerce_value(value: Any, schema: dict[str, Any]) -> Any:
    expected = _expected_type(schema)
    if _schema_allows_null(schema) and isinstance(value, str) and value.strip().lower() == "null":
        return None
    if expected == "array":
        if isinstance(value, list):
            return _coerce_array_items(value, schema)
        if isinstance(value, str):
            parsed = _coerce_json(value, list)
            if isinstance(parsed, list):
                return _coerce_array_items(parsed, schema)
        return _coerce_array_items([value], schema)
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


def _coerce_array_items(values: list[Any], schema: dict[str, Any]) -> list[Any]:
    item_schema = _array_item_schema(schema)
    if not isinstance(item_schema, dict):
        return values
    coerced: list[Any] = []
    for item in values:
        if item_schema.get("type") == "string" and item is not None and not isinstance(item, str):
            coerced.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
            continue
        coerced.append(_coerce_value(item, item_schema))
    return coerced


def _array_item_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    items = schema.get("items")
    if isinstance(items, dict):
        return items
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            nested_items = variant.get("items")
            if isinstance(nested_items, dict):
                return nested_items
    return None


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
    stripped = value.strip()
    if not re.fullmatch(r"[+-]?\d+", stripped):
        return value
    try:
        return int(stripped)
    except (TypeError, ValueError, OverflowError):
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
