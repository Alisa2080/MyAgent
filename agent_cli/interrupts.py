from __future__ import annotations

from typing import Any

from agent_cli.approval import ApprovalRequest

INTERRUPT_KEY = "__interrupt__"


def has_interrupt(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get(INTERRUPT_KEY))


def _unwrap_interrupt_value(item: Any) -> Any:
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    if hasattr(item, "value"):
        return item.value
    return item


def extract_interrupt_review_requests(result: Any) -> list[ApprovalRequest]:
    if not has_interrupt(result):
        return []
    raw_items = result.get(INTERRUPT_KEY) or []
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    paired: list[ApprovalRequest] = []
    for item in raw_items:
        value = _unwrap_interrupt_value(item)
        if not isinstance(value, dict):
            paired.append(ApprovalRequest({"raw": value}, {}))
            continue

        action_requests = value.get("action_requests")
        review_configs = value.get("review_configs") or []
        if not isinstance(action_requests, list):
            paired.append(ApprovalRequest(value, {}))
            continue
        if not action_requests:
            paired.append(ApprovalRequest(value, {}))
            continue

        if not isinstance(review_configs, list):
            review_configs = []

        for index, request in enumerate(action_requests):
            action = request if isinstance(request, dict) else {"raw": request}
            config = review_configs[index] if index < len(review_configs) else {}
            if not isinstance(config, dict):
                config = {"raw": config}
            paired.append(ApprovalRequest(action, config))
    return paired


def extract_interrupt_requests(result: Any) -> list[dict[str, Any]]:
    requests = extract_interrupt_review_requests(result)
    return [item.action_request for item in requests] or [{"raw": result.get(INTERRUPT_KEY)}]
