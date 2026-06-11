from __future__ import annotations

from typing import Any

from agent_core.permissions import tool_policy


def terminal_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("terminal", args)


def process_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("process", args)


def write_file_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("write_file", args)


def patch_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("patch", args)


POLICY_ARG_BUILDERS = {
    "terminal": terminal_policy_args,
    "process": process_policy_args,
    "write_file": write_file_policy_args,
    "patch": patch_policy_args,
}
