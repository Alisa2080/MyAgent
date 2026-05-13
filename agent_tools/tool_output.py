import json
from typing import Any


def tool_ok(tool: str, *, data: Any = None, message: str = "", meta: dict[str, Any] | None = None) -> str:
    payload = {
        "ok": True,
        "tool": tool,
        "message": message,
        "data": data,
        "error": None,
        "meta": meta or {},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def tool_error(
    tool: str,
    message: str,
    *,
    code: str = "tool_error",
    data: Any = None,
    meta: dict[str, Any] | None = None,
) -> str:
    payload = {
        "ok": False,
        "tool": tool,
        "message": message,
        "data": data,
        "error": {
            "code": code,
            "message": message,
        },
        "meta": meta or {},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
