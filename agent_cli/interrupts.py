from __future__ import annotations

from typing import Any


INTERRUPT_KEY = "__interrupt__"


def has_interrupt(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get(INTERRUPT_KEY))


def _unwrap_interrupt_value(item: Any) -> Any:
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    if hasattr(item, "value"):
        return item.value
    return item


def extract_interrupt_requests(result: Any) -> list[dict[str, Any]]:
    if not has_interrupt(result):
        return []
    raw_items = result.get(INTERRUPT_KEY) or []
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    requests: list[dict[str, Any]] = []
    for item in raw_items:
        value = _unwrap_interrupt_value(item)
        if isinstance(value, dict):
            action_requests = value.get("action_requests")
            if isinstance(action_requests, list):
                for request in action_requests:
                    if isinstance(request, dict):
                        requests.append(request)
                    else:
                        requests.append({"raw": request})
                continue
            requests.append(value)
        else:
            requests.append({"raw": value})
    return requests or [{"raw": raw_items}]


def build_resume_value(
    *, approved: bool, request_count: int
) -> dict[str, list[dict[str, str]]]:
    count = max(1, request_count)
    if approved:
        return {"decisions": [{"type": "approve"} for _ in range(count)]}
    return {
        "decisions": [
            {"type": "reject", "message": "Rejected by user."} for _ in range(count)
        ]
    }
