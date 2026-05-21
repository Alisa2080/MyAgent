from __future__ import annotations

from typing import Any

from agent_core.permissions import command_policy, file_policy
from agent_core.permissions.models import PolicyDecision
from agent_tools.file_toolkit.patch_parser import parse_v4a_patch


_READ_TOOLS = {"read_file", "search_files", "list_directory", "file_info"}
_MANDATORY_REVIEW_TOOLS = {"memory_manage", "skill_manage"}
_PROCESS_STDIN_ACTIONS = {"write", "submit"}


def _patch_paths(args: dict[str, Any]) -> list[str]:
    mode = args.get("mode", "replace")
    if mode == "replace":
        path = args.get("path")
        return [str(path)] if path else []
    if mode != "patch":
        return []

    operations, error = parse_v4a_patch(args.get("patch") or "")
    if error:
        return []

    paths: list[str] = []
    for operation in operations:
        if operation.file_path:
            paths.append(operation.file_path)
        if operation.new_path:
            paths.append(operation.new_path)
    return paths


def _combine_write_decisions(decisions: list[PolicyDecision]) -> PolicyDecision:
    for decision in decisions:
        if decision.outcome == "deny":
            return decision

    reviews = [decision for decision in decisions if decision.outcome == "review"]
    if reviews:
        first_review = reviews[0]
        risk_tags: list[str] = []
        for decision in reviews:
            for risk_tag in decision.risk_tags:
                if risk_tag not in risk_tags:
                    risk_tags.append(risk_tag)
        return PolicyDecision.review(
            first_review.reason,
            risk_tags=tuple(risk_tags),
            requires_network=any(decision.requires_network for decision in reviews),
            message=first_review.human_message,
            data={"decisions": [dict(decision.data) for decision in decisions]},
        )

    return PolicyDecision.allow("workspace_write")


def evaluate_tool_call(
    tool_name: str,
    args: dict[str, Any],
    task_id: str,
    tool_call_id: str | None = None,
) -> PolicyDecision:
    del tool_call_id

    if tool_name in _READ_TOOLS:
        return PolicyDecision.allow("read_tool")

    if tool_name in _MANDATORY_REVIEW_TOOLS:
        return PolicyDecision.review("mandatory_review", risk_tags=("mandatory_review",))

    if tool_name == "write_file":
        return file_policy.classify_file_write(str(args.get("path") or ""), task_id=task_id)

    if tool_name == "patch":
        paths = _patch_paths(args)
        if not paths:
            return PolicyDecision.review(
                "patch_paths_unresolved",
                risk_tags=("path_resolution_failed",),
            )
        return _combine_write_decisions(
            [file_policy.classify_file_write(path, task_id=task_id) for path in paths]
        )

    if tool_name == "terminal":
        return command_policy.classify_command(
            str(args.get("command") or ""),
            background=bool(args.get("background", False)),
        )

    if tool_name == "process":
        if args.get("action") in _PROCESS_STDIN_ACTIONS:
            return PolicyDecision.review("process_stdin", risk_tags=("process_stdin",))
        return PolicyDecision.allow("process_control")

    return PolicyDecision.allow("unmanaged_tool")
