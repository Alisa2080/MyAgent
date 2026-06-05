from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalRequest:
    action_request: dict[str, Any]
    review_config: dict[str, Any]

    @property
    def tool_name(self) -> str:
        name = self.action_request.get("name")
        return str(name) if name else "unknown"

    @property
    def args(self) -> Any:
        return self.action_request.get("args") or {}

    @property
    def description(self) -> str:
        value = self.review_config.get("description") or self.review_config.get("risk")
        return str(value) if value else ""


def summarize_args(args: Any, *, max_length: int = 160) -> str:
    if isinstance(args, dict):
        for key in ("command", "path", "file_path", "name", "action"):
            if key in args and args[key] not in (None, ""):
                text = f'{key}="{args[key]}"'
                return text if len(text) <= max_length else text[: max_length - 3] + "..."
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(args)
    return text if len(text) <= max_length else text[: max_length - 3] + "..."


def _message_or_default(value: str, default: str) -> str:
    value = value.strip()
    return value or default


def _parse_json_args(raw: str) -> Any:
    parsed = json.loads(raw)
    if not isinstance(parsed, (dict, list)):
        raise ValueError("Edited args must be a JSON object or array.")
    return parsed


def _render_request(index: int, total: int, request: ApprovalRequest) -> str:
    lines = [
        f"[{index}/{total}] {request.tool_name}",
        f"Args: {summarize_args(request.args)}",
    ]
    if request.description:
        lines.append(f"Risk: {request.description}")
    lines.append("")
    return "\n".join(lines)


def _is_clarify_request(request: ApprovalRequest) -> bool:
    return request.tool_name == "clarify" or request.review_config.get("kind") == "clarify"


def _clarify_choices(request: ApprovalRequest) -> list[str]:
    args = request.args if isinstance(request.args, dict) else {}
    raw_choices = args.get("choices")
    if not isinstance(raw_choices, list):
        return []
    return [str(choice).strip() for choice in raw_choices if str(choice).strip()]


def _clarify_question(request: ApprovalRequest) -> str:
    args = request.args if isinstance(request.args, dict) else {}
    question = str(args.get("question") or "").strip()
    return question or "Clarification needed."


def _render_clarify_request(index: int, total: int, request: ApprovalRequest) -> str:
    lines = [f"[{index}/{total}] clarify", _clarify_question(request), ""]
    choices = _clarify_choices(request)
    for choice_index, choice in enumerate(choices, start=1):
        lines.append(f"{choice_index}. {choice}")
    if choices:
        lines.append(f"{len(choices) + 1}. Other (type your answer)")
        lines.append("")
    return "\n".join(lines)


def _clarify_decision_from_answer(request: ApprovalRequest, answer: str) -> dict[str, str]:
    text = str(answer or "").strip()
    choices = _clarify_choices(request)
    if choices and text.isdigit():
        selected = int(text)
        if 1 <= selected <= len(choices):
            text = choices[selected - 1]
    if not text:
        text = "No clarification answer provided."
    return {"type": "respond", "message": text}


def _is_clarify_other_choice(request: ApprovalRequest, answer: str) -> bool:
    choices = _clarify_choices(request)
    text = str(answer or "").strip()
    return bool(choices and text.isdigit() and int(text) == len(choices) + 1)


def collect_approval_decisions(
    requests: list[ApprovalRequest],
    *,
    input_func=input,
    print_func=print,
) -> dict[str, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    total = len(requests)
    index = 0
    eof_message = "Rejected because approval input ended."
    while index < total:
        request = requests[index]
        if _is_clarify_request(request):
            print_func(_render_clarify_request(index + 1, total, request))
            try:
                answer = input_func("Answer: ")
            except EOFError:
                decisions.extend(
                    {"type": "reject", "message": eof_message}
                    for _ in range(total - index)
                )
                break
            if _is_clarify_other_choice(request, answer):
                try:
                    answer = input_func("Other answer: ")
                except EOFError:
                    decisions.extend(
                        {"type": "reject", "message": eof_message}
                        for _ in range(total - index)
                    )
                    break
            decisions.append(_clarify_decision_from_answer(request, answer))
            index += 1
            continue
        print_func(_render_request(index + 1, total, request))
        try:
            answer = input_func("Decision [y/n/e/r/a/q]: ").strip().lower()
        except EOFError:
            decisions.extend(
                {"type": "reject", "message": eof_message}
                for _ in range(total - index)
            )
            break

        if answer in {"y", "yes"}:
            decisions.append({"type": "approve"})
            index += 1
            continue
        if answer in {"a", "all"}:
            decisions.extend({"type": "approve"} for _ in range(total - index))
            break
        if answer in {"n", "no"}:
            try:
                message = _message_or_default(
                    input_func("Reject message: "),
                    "Rejected by user.",
                )
            except EOFError:
                decisions.extend(
                    {"type": "reject", "message": eof_message}
                    for _ in range(total - index)
                )
                break
            decisions.append({"type": "reject", "message": message})
            index += 1
            continue
        if answer in {"q", "quit"}:
            try:
                message = _message_or_default(
                    input_func("Reject message for all remaining: "),
                    "Rejected by user.",
                )
            except EOFError:
                message = eof_message
            decisions.extend(
                {"type": "reject", "message": message}
                for _ in range(total - index)
            )
            break
        if answer in {"r", "respond"}:
            try:
                message = _message_or_default(
                    input_func("Response message: "),
                    "Please revise the request.",
                )
            except EOFError:
                decisions.extend(
                    {"type": "reject", "message": eof_message}
                    for _ in range(total - index)
                )
                break
            decisions.append({"type": "respond", "message": message})
            index += 1
            continue
        if answer in {"e", "edit"}:
            print_func("Current args JSON:")
            print_func(json.dumps(request.args, ensure_ascii=False, indent=2, sort_keys=True))
            try:
                raw = input_func("Edited args JSON: ")
            except EOFError:
                decisions.extend(
                    {"type": "reject", "message": eof_message}
                    for _ in range(total - index)
                )
                break
            try:
                edited_args = _parse_json_args(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print_func(f"Invalid JSON: {exc}")
                continue
            decisions.append(
                {
                    "type": "edit",
                    "edited_action": {
                        "name": request.tool_name,
                        "args": edited_args,
                    },
                }
            )
            index += 1
            continue

        print_func("Choose y, n, e, r, a, or q.")

    return {"decisions": decisions}
