import json
import os
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.permissions import file_policy
from agent_core.permissions.approvals import consume_approval
from agent_core.permissions.constants import UNRESOLVED_BACKEND_WRITE_PATH
from agent_core.permissions.models import PolicyDecision
from agent_core.permissions.tool_grants import consume_tool_policy_grant
from agent_core.policy_tool_middleware import patch_policy_args, write_file_policy_args
from agent_core.session_context import RuntimeContext
from agent_core.workspace import WORKDIR, safe_path
from agent_tools.file_toolkit.backend_paths import (
    allowed_workspace_roots_for_task,
    get_backend_path_context,
    path_is_under_any_root,
    resolve_path_for_policy,
)
from agent_tools.file_toolkit.file_tools import (
    _get_file_ops,
    patch_tool,
    read_file_tool,
    search_tool,
    write_file_tool,
)
from agent_tools.file_toolkit.patch_parser import parse_v4a_patch
from agent_tools.shared.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path
from agent_tools.shared.tool_result import (
    ToolMessage,
    from_legacy_json,
    tool_failure,
    tool_success,
)


class ListDirectoryInput(BaseModel):
    path: str = Field(default=".", description="Directory path to list.")
    recursive: bool = Field(default=False, description="Whether to list entries recursively.")
    include_hidden: bool = Field(default=False, description="Whether to include hidden dotfiles and dot-directories.")
    limit: int = Field(default=200, description="Maximum number of entries to return.")


class ReadFileInput(BaseModel):
    path: str = Field(description="Path to the text file to read.")
    offset: int = Field(default=1, ge=1, description="1-indexed start line.")
    limit: int = Field(default=500, ge=1, le=2000, description="Maximum number of lines to read.")


class WriteFileInput(BaseModel):
    path: str = Field(description="Path to write inside the workspace.")
    content: str = Field(description="Complete file content. This overwrites the entire file.")


class PatchInput(BaseModel):
    mode: Literal["replace", "patch"] = Field(
        default="replace",
        description="Use replace for targeted old_string/new_string edits, or patch for V4A multi-file patches.",
    )
    path: str | None = Field(default=None, description="Required for replace mode.")
    old_string: str | None = Field(default=None, description="Text to replace in replace mode.")
    new_string: str | None = Field(default=None, description="Replacement text in replace mode.")
    replace_all: bool = Field(default=False, description="Replace all matches instead of requiring a unique match.")
    patch: str | None = Field(default=None, description="V4A patch content for patch mode.")


class SearchFilesInput(BaseModel):
    pattern: str = Field(description="Regex for content search or glob for file search.")
    target: Literal["content", "files"] = Field(default="content", description="Search file contents or filenames.")
    path: str = Field(default=".", description="Workspace path to search from.")
    file_glob: str | None = Field(default=None, description="Optional filename glob filter for content search.")
    limit: int = Field(default=50, ge=1, description="Maximum number of results to return.")
    offset: int = Field(default=0, ge=0, description="Skip the first N results for pagination.")
    output_mode: Literal["content", "files_only", "count"] = Field(
        default="content",
        description="Output format for content search.",
    )
    context: int = Field(default=0, ge=0, description="Context lines before and after each content match.")


class FileInfoInput(BaseModel):
    path: str = Field(description="Path to the file or directory to inspect.")


def _wrap_file_tool_result(
    tool_name: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
    runtime: ToolRuntime | None = None,
    summary=None,
    observation=None,
) -> ToolMessage:
    return from_legacy_json(
        tool_name,
        raw,
        success_message=success_message,
        meta_keys=meta_keys,
        runtime=runtime,
        summary=summary,
        observation=observation,
    )


def _read_file_summary(payload: dict, meta: dict) -> str:
    path = payload.get("path") or "file"
    start = payload.get("start_line") or payload.get("offset")
    end = payload.get("end_line")
    if start and end:
        return f"Read {path} lines {start}-{end}."
    return f"Read {path}."


def _read_file_observation(payload: dict, meta: dict) -> str:
    path = payload.get("path") or "file"
    content = str(payload.get("content") or "")
    start = payload.get("start_line") or payload.get("offset")
    end = payload.get("end_line")
    if start and end:
        header = f"Read {path} lines {start}-{end}:"
    else:
        header = f"Read {path}:"
    parts = [header, content]
    if meta.get("truncated"):
        parts.append("[truncated]")
    if meta.get("hint"):
        parts.append(str(meta["hint"]))
    return "\n".join(part for part in parts if part)


def _search_files_summary(payload: dict, meta: dict) -> str:
    total = (
        payload.get("total")
        or payload.get("total_count")
        or payload.get("match_count")
        or payload.get("count")
    )
    if total is not None:
        suffix = " Output was truncated." if meta.get("truncated") else ""
        return f"Search completed with {total} result(s).{suffix}"
    return "Search completed."


def _search_files_observation(payload: dict, meta: dict) -> str:
    pattern = payload.get("pattern") or payload.get("query") or ""
    matches = payload.get("matches") or payload.get("results") or []
    files = payload.get("files") or []
    counts = payload.get("counts") or {}
    total = (
        payload.get("total")
        or payload.get("total_count")
        or payload.get("match_count")
        or payload.get("count")
    )
    lines = [f"Search results for {pattern!r}:".rstrip()]
    target = payload.get("target")
    path = payload.get("path")
    file_glob = payload.get("file_glob")
    output_mode = payload.get("output_mode")
    if target:
        lines.append(f"Target: {target}")
    if path:
        lines.append(f"Path: {path}")
    if file_glob:
        lines.append(f"File glob: {file_glob}")
    if output_mode:
        lines.append(f"Output mode: {output_mode}")
    if total is not None:
        lines.append(f"Total: {total}")
    if isinstance(matches, list):
        for item in matches:
            if not isinstance(item, dict):
                lines.append(str(item))
                continue
            path = item.get("path") or item.get("file") or item.get("filename") or "(unknown)"
            line = item.get("line") or item.get("line_number") or item.get("line_start")
            content = item.get("content") or item.get("text") or item.get("match") or ""
            location = f"{path}:{line}" if line else str(path)
            lines.append(f"- {location}")
            if content:
                lines.append(f"  {content}")
    if isinstance(files, list):
        for path_item in files:
            lines.append(f"- {path_item}")
    if isinstance(counts, dict) and counts:
        lines.append("Counts:")
        for key, value in sorted(counts.items()):
            lines.append(f"- {key}: {value}")
    if meta.get("truncated"):
        lines.append("[truncated]")
    if meta.get("hint"):
        lines.append(str(meta["hint"]))
    return "\n".join(lines)


def _list_directory_observation(payload: dict, meta: dict) -> str:
    path = payload.get("path") or "."
    entries = payload.get("entries") or []
    total = payload.get("total")
    lines = [f"Directory listing for {path}:"]
    if total is not None:
        lines.append(f"Total: {total}")
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                entry_type = item.get("type") or "entry"
                entry_path = item.get("path") or item.get("name") or ""
                lines.append(f"- {entry_type}: {entry_path}")
            else:
                lines.append(f"- {item}")
    if meta.get("truncated"):
        lines.append("[truncated]")
    return "\n".join(lines)


def _runtime_context(runtime: ToolRuntime | None) -> RuntimeContext:
    return RuntimeContext.from_runtime(runtime)


def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return _runtime_context(runtime).task_id


def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    return _runtime_context(runtime).tool_call_id


def _allowed_workspace_roots_for_task(task_id: str) -> list[str]:
    return allowed_workspace_roots_for_task(task_id)


def _is_under_allowed_workspace_root(resolved: str, task_id: str) -> bool:
    return path_is_under_any_root(resolved, _allowed_workspace_roots_for_task(task_id))


def _blocked_read_dirs_for_task(task_id: str) -> list[str]:
    blocked_dirs: list[str] = []
    seen: set[str] = set()
    for root in _allowed_workspace_roots_for_task(task_id):
        for blocked_dir in (
            posixpath.join(str(root), "skills", ".hub"),
            posixpath.join(str(root), "skills", ".hub", "index-cache"),
        ):
            resolved = posixpath.normpath(blocked_dir)
            if resolved not in seen:
                blocked_dirs.append(resolved)
                seen.add(resolved)
    return blocked_dirs


def _ensure_workspace_path_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = resolve_path_for_policy(path or ".", task_id)
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"
    return None


def _ensure_read_allowed_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = resolve_path_for_policy(path or ".", task_id)
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"

    if path_is_under_any_root(resolved, _blocked_read_dirs_for_task(task_id)):
        return (
            f"Access denied: {path} is an internal skill cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None


def _ensure_patch_paths_for_task(patch_content: str | None, task_id: str) -> str | None:
    if not patch_content:
        return None

    operations, parse_error = parse_v4a_patch(patch_content)
    if not parse_error:
        for operation in operations:
            path_error = _ensure_workspace_path_for_task(operation.file_path, task_id)
            if path_error:
                return path_error
            if operation.new_path:
                path_error = _ensure_workspace_path_for_task(operation.new_path, task_id)
                if path_error:
                    return path_error

    for match in re.finditer(
        r"^\*\*\*\s+Move to:\s*(.+)$",
        patch_content,
        re.MULTILINE,
    ):
        path_error = _ensure_workspace_path_for_task(match.group(1).strip(), task_id)
        if path_error:
            return path_error
    return None


def _approval_roots_for_file_decision(
    tool_name: str,
    path: str,
    args: dict,
    task_id: str,
    runtime: ToolRuntime | None,
    decision: PolicyDecision,
    resolved_path: str | None = None,
) -> list[str] | str:
    if decision.outcome == "allow":
        consume_tool_policy_grant(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name=tool_name,
            args=args,
        )
        return []
    if decision.outcome == "deny":
        return decision.human_message

    grant = consume_tool_policy_grant(
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
        tool_name=tool_name,
        args=args,
        required_risk_tags=decision.risk_tags,
    )
    if grant is not None:
        return [file_policy.approved_write_root_for_path(path, task_id=task_id, resolved_path=resolved_path)]

    approval = consume_approval(
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
        tool_name=tool_name,
        args=args,
        required_risk_tags=decision.risk_tags,
    )
    if approval is None:
        return "Approval required before writing outside the workspace."
    return [file_policy.approved_write_root_for_path(path, task_id=task_id, resolved_path=resolved_path)]


def _file_policy_error_code(decision: PolicyDecision) -> str:
    return "policy_denied" if decision.outcome == "deny" else "approval_required"


def _resolved_write_path_for_policy(path: str, task_id: str) -> str | None:
    try:
        if get_backend_path_context(task_id).env_type == "local":
            return None
        resolver = getattr(_get_file_ops(task_id), "_resolve_write_safety_path", None)
        if resolver is None:
            return UNRESOLVED_BACKEND_WRITE_PATH
        return str(resolver(path))
    except Exception:
        return UNRESOLVED_BACKEND_WRITE_PATH


def _unique_items(items: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        unique.append(item)
        seen.add(item)
    return unique


def _review_risk_tags(decisions: list[PolicyDecision]) -> tuple[str, ...]:
    risk_tags: list[str] = []
    for decision in decisions:
        if decision.outcome == "review":
            risk_tags.extend(decision.risk_tags)
    return tuple(_unique_items(risk_tags))


def _patch_paths_to_classify(
    mode: str,
    path: str | None,
    patch_content: str | None,
) -> tuple[list[str], str | None]:
    if mode == "replace":
        return ([path] if path else []), None
    if mode != "patch" or patch_content is None:
        return [], None

    operations, parse_error = parse_v4a_patch(patch_content)
    if parse_error:
        return [], parse_error

    paths: list[str] = []
    for operation in operations:
        if operation.file_path:
            paths.append(operation.file_path)
        if operation.new_path:
            paths.append(operation.new_path)
    for match in re.finditer(
        r"^\*\*\*\s+Move to:\s*(.+)$",
        patch_content,
        re.MULTILINE,
    ):
        paths.append(match.group(1).strip())
    return _unique_items(paths), None


def _utc_iso(timestamp: str) -> str:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()


def _backend_entry_is_visible(path: str, include_hidden: bool) -> bool:
    parts = [part for part in posixpath.normpath(path).split("/") if part]
    if any(part in DEFAULT_EXCLUDE_DIRS for part in parts):
        return False
    if not include_hidden and any(part.startswith(".") for part in parts):
        return False
    if posixpath.basename(path).endswith(".bak"):
        return False
    return True


def _parse_find_entry(line: str) -> tuple[str, str]:
    if "\t" in line:
        type_char, entry_path = line.split("\t", 1)
        return ("directory" if type_char == "d" else "file", entry_path)
    entry_path = line.strip()
    basename = posixpath.basename(entry_path)
    return ("file" if "." in basename else "directory", entry_path)


def _list_directory_backend(
    path: str,
    resolved_path: str,
    *,
    recursive: bool,
    include_hidden: bool,
    limit: int,
    task_id: str,
    runtime: ToolRuntime | None = None,
) -> ToolMessage:
    file_ops = _get_file_ops(task_id)
    quoted = file_ops._escape_shell_arg(resolved_path)
    if file_ops._exec(f"if [ ! -e {quoted} ]; then exit 1; fi", timeout=10).exit_code != 0:
        return tool_failure(
            "list_directory",
            f"Directory not found: {path}",
            code="not_found",
            runtime=runtime,
        )
    if file_ops._exec(f"if [ ! -d {quoted} ]; then exit 1; fi", timeout=10).exit_code != 0:
        return tool_failure(
            "list_directory",
            f"Not a directory: {path}",
            code="not_directory",
            runtime=runtime,
        )

    depth_arg = "" if recursive else "-maxdepth 1"
    result = file_ops._exec(
        f"find {quoted} -mindepth 1 {depth_arg} -printf '%y\\t%p\\n' 2>/dev/null",
        timeout=30,
    )
    if result.exit_code != 0:
        return tool_failure(
            "list_directory",
            "Failed to list directory.",
            code="tool_error",
            runtime=runtime,
        )

    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry_type, entry_path = _parse_find_entry(line)
        if _backend_entry_is_visible(entry_path, include_hidden):
            entries.append((entry_type, entry_path))

    entries.sort(key=lambda item: (item[0] == "file", item[1].lower()))
    max_entries = max(limit, 0)
    items = [
        {"type": entry_type, "path": entry_path}
        for entry_type, entry_path in entries[:max_entries]
    ]
    total = len(entries)
    return tool_success(
        "list_directory",
        data={"path": path, "entries": items, "total": total},
        message="Directory listed.",
        meta={"truncated": total > len(items)},
        runtime=runtime,
        content="Directory listed.",
    )


def _list_directory_impl(
    path: str = ".",
    recursive: bool = False,
    include_hidden: bool = False,
    limit: int = 200,
    runtime: ToolRuntime | None = None,
) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_failure("list_directory", read_error, code=code, runtime=runtime)

    ctx = get_backend_path_context(task_id)
    resolved_path = resolve_path_for_policy(path, task_id)
    if ctx.env_type != "local":
        return _list_directory_backend(
            path,
            resolved_path,
            recursive=recursive,
            include_hidden=include_hidden,
            limit=limit,
            task_id=task_id,
            runtime=runtime,
        )

    try:
        dir_path = safe_path(path)
        if not dir_path.exists():
            return tool_failure(
                "list_directory",
                f"Directory not found: {path}",
                code="not_found",
                runtime=runtime,
            )
        if not dir_path.is_dir():
            return tool_failure(
                "list_directory",
                f"Not a directory: {path}",
                code="not_directory",
                runtime=runtime,
            )

        if recursive:
            entries = []
            for dirpath, dirnames, filenames in os.walk(dir_path):
                current_dir = Path(dirpath)
                dirnames[:] = [
                    dirname
                    for dirname in dirnames
                    if dirname not in DEFAULT_EXCLUDE_DIRS and (include_hidden or not dirname.startswith("."))
                ]
                entries.extend(current_dir / dirname for dirname in dirnames)
                entries.extend(current_dir / filename for filename in filenames)
        else:
            entries = list(dir_path.iterdir())

        items: list[dict[str, str]] = []
        total = 0
        max_entries = max(limit, 0)
        for entry in sorted(entries, key=lambda item: (item.is_file(), item.name.lower())):
            relative_parts = entry.relative_to(WORKDIR).parts
            if entry.is_file() and entry.name.endswith(".bak"):
                continue
            if not include_hidden and any(part.startswith(".") for part in relative_parts):
                continue
            if any(part in DEFAULT_EXCLUDE_DIRS for part in relative_parts):
                continue
            total += 1
            if len(items) < max_entries:
                items.append({"type": "directory" if entry.is_dir() else "file", "path": relative_path(entry)})

        return tool_success(
            "list_directory",
            data={"path": path, "entries": items, "total": total},
            message="Directory listed.",
            meta={"truncated": total > len(items)},
            runtime=runtime,
            content=_list_directory_observation(
                {"path": path, "entries": items, "total": total},
                {"truncated": total > len(items)},
            ),
        )
    except Exception as exc:
        return tool_failure("list_directory", str(exc), runtime=runtime)


@tool("list_directory", args_schema=ListDirectoryInput)
def list_directory(
    runtime: ToolRuntime,
    path: str = ".",
    recursive: bool = False,
    include_hidden: bool = False,
    limit: int = 200,
) -> ToolMessage:
    """List files and directories inside the workspace."""
    return _list_directory_impl(
        path=path,
        recursive=recursive,
        include_hidden=include_hidden,
        limit=limit,
        runtime=runtime,
    )


def _read_file_impl(path: str, offset: int = 1, limit: int = 500, runtime: ToolRuntime | None = None) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_failure("read_file", read_error, code=code, runtime=runtime)
    raw = read_file_tool(path=path, offset=offset, limit=limit, task_id=task_id)
    return _wrap_file_tool_result(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
        runtime=runtime,
        summary=_read_file_summary,
        observation=_read_file_observation,
    )


@tool("read_file", args_schema=ReadFileInput)
def read_file(path: str, runtime: ToolRuntime, offset: int = 1, limit: int = 500) -> ToolMessage:
    """Read a text file with line numbers and pagination."""
    return _read_file_impl(path=path, offset=offset, limit=limit, runtime=runtime)


def _write_file_impl(path: str, content: str, runtime: ToolRuntime | None = None) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    resolved_path = _resolved_write_path_for_policy(path, task_id)
    decision = file_policy.classify_file_write(path, task_id=task_id, resolved_path=resolved_path)
    approved_roots = _approval_roots_for_file_decision(
        tool_name="write_file",
        path=path,
        args=write_file_policy_args({"path": path, "content": content}),
        task_id=task_id,
        runtime=runtime,
        decision=decision,
        resolved_path=resolved_path,
    )
    if isinstance(approved_roots, str):
        return tool_failure(
            "write_file",
            approved_roots,
            code=_file_policy_error_code(decision),
            data=decision.data,
            runtime=runtime,
        )
    kwargs = {"path": path, "content": content, "task_id": task_id}
    if approved_roots:
        kwargs["approved_write_roots"] = approved_roots
    raw = write_file_tool(**kwargs)
    return _wrap_file_tool_result("write_file", raw, success_message="File written.", runtime=runtime)


@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str, runtime: ToolRuntime) -> ToolMessage:
    """Write complete content to a workspace file, replacing existing content."""
    return _write_file_impl(path=path, content=content, runtime=runtime)


def _patch_impl(
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    if mode == "replace":
        if not path:
            return tool_failure("patch", "path is required for replace mode.", code="invalid_input", runtime=runtime)
    paths_to_classify, parse_error = _patch_paths_to_classify(mode, path, patch)
    if parse_error:
        return tool_failure("patch", f"Failed to parse patch: {parse_error}", code="invalid_input", runtime=runtime)

    resolved_paths = [
        _resolved_write_path_for_policy(path_to_classify, task_id)
        for path_to_classify in paths_to_classify
    ]
    decisions = [
        file_policy.classify_file_write(path_to_classify, task_id=task_id, resolved_path=resolved_path)
        for path_to_classify, resolved_path in zip(paths_to_classify, resolved_paths, strict=False)
    ]
    denied = next((decision for decision in decisions if decision.outcome == "deny"), None)
    if denied is not None:
        return tool_failure(
            "patch",
            denied.human_message,
            code="policy_denied",
            data=denied.data,
            runtime=runtime,
        )

    approval_args = patch_policy_args(
        {
            "mode": mode,
            "path": path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
            "patch": patch,
        }
    )
    approved_roots: list[str] = []
    review = next((decision for decision in decisions if decision.outcome == "review"), None)
    if review is not None:
        review_risk_tags = _review_risk_tags(decisions)
        grant = consume_tool_policy_grant(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="patch",
            args=approval_args,
            required_risk_tags=review_risk_tags,
        )
        if grant is None and consume_approval(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="patch",
            args=approval_args,
            required_risk_tags=review_risk_tags,
        ) is None:
            return tool_failure(
                "patch",
                "Approval required before patching outside the workspace.",
                code="approval_required",
                data=review.data,
                runtime=runtime,
            )
        approved_roots = _unique_items(
            [
                file_policy.approved_write_root_for_path(
                    path_to_classify,
                    task_id=task_id,
                    resolved_path=resolved_path,
                )
                for path_to_classify, resolved_path, decision in zip(
                    paths_to_classify,
                    resolved_paths,
                    decisions,
                    strict=False,
                )
                if decision.outcome == "review"
            ]
        )
    else:
        consume_tool_policy_grant(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="patch",
            args=approval_args,
        )

    kwargs = {
        "mode": mode,
        "path": path,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
        "patch": patch,
        "task_id": task_id,
    }
    if approved_roots:
        kwargs["approved_write_roots"] = approved_roots
    raw = patch_tool(**kwargs)
    return _wrap_file_tool_result("patch", raw, success_message="Patch applied.", runtime=runtime)


@tool("patch", args_schema=PatchInput)
def patch(
    runtime: ToolRuntime,
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
) -> ToolMessage:
    """Apply targeted file edits. Prefer replace mode for small edits."""
    return _patch_impl(
        mode=mode,
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        patch=patch,
        runtime=runtime,
    )


def _search_files_impl(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
    runtime: ToolRuntime | None = None,
) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_failure("search_files", read_error, code=code, runtime=runtime)
    raw = search_tool(
        pattern=pattern,
        target=target,
        path=path,
        file_glob=file_glob,
        limit=limit,
        offset=offset,
        output_mode=output_mode,
        context=context,
        task_id=task_id,
    )
    try:
        raw_payload = json.loads(raw)
    except json.JSONDecodeError:
        raw_payload = None
    if isinstance(raw_payload, dict) and "error" not in raw_payload:
        raw_payload.setdefault("pattern", pattern)
        raw_payload.setdefault("target", target)
        raw_payload.setdefault("path", path)
        raw_payload.setdefault("file_glob", file_glob)
        raw_payload.setdefault("output_mode", output_mode)
        raw = json.dumps(raw_payload, ensure_ascii=False)
    return _wrap_file_tool_result(
        "search_files",
        raw,
        success_message="Search completed.",
        meta_keys=("truncated",),
        runtime=runtime,
        summary=_search_files_summary,
        observation=_search_files_observation,
    )


@tool("search_files", args_schema=SearchFilesInput)
def search_files(
    pattern: str,
    runtime: ToolRuntime,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
) -> ToolMessage:
    """Search workspace file contents or find files by name."""
    return _search_files_impl(
        pattern=pattern,
        target=target,
        path=path,
        file_glob=file_glob,
        limit=limit,
        offset=offset,
        output_mode=output_mode,
        context=context,
        runtime=runtime,
    )


def _file_info_backend(path: str, resolved_path: str, *, task_id: str, runtime: ToolRuntime | None = None) -> ToolMessage:
    file_ops = _get_file_ops(task_id)
    quoted = file_ops._escape_shell_arg(resolved_path)
    if file_ops._exec(f"if [ ! -e {quoted} ]; then exit 1; fi", timeout=10).exit_code != 0:
        return tool_failure(
            "file_info",
            f"Path not found: {path}",
            code="not_found",
            data={"path": path},
            runtime=runtime,
        )

    result = file_ops._exec(
        f"stat -c '%n\\t%F\\t%s\\t%Y\\t%Z' {quoted}",
        timeout=10,
    )
    if result.exit_code != 0 or not result.stdout.strip():
        return tool_failure(
            "file_info",
            "Failed to inspect path.",
            code="tool_error",
            data={"path": path},
            runtime=runtime,
        )

    stat_line = result.stdout.splitlines()[0]
    parts = stat_line.split("\t", 4)
    if len(parts) != 5:
        return tool_failure(
            "file_info",
            "Unexpected stat output.",
            code="tool_error",
            data={"path": path},
            runtime=runtime,
        )

    backend_path, type_text, size_text, modified, created = parts
    info = {
        "path": backend_path,
        "type": "directory" if type_text == "directory" else "file",
        "size_bytes": int(size_text),
        "modified_utc": _utc_iso(modified),
        "created_utc": _utc_iso(created),
    }
    if info["type"] == "directory":
        entries_result = file_ops._exec(
            f"find {quoted} -mindepth 1 -maxdepth 1 -printf '.\\n' 2>/dev/null | wc -l",
            timeout=10,
        )
        if entries_result.exit_code == 0:
            try:
                info["entries"] = int(entries_result.stdout.strip() or "0")
            except ValueError:
                info["entries"] = 0
    return tool_success(
        "file_info",
        data=info,
        message="Path inspected.",
        runtime=runtime,
        content=f"Inspected {path}.",
    )


def _file_info_impl(path: str, runtime: ToolRuntime | None = None) -> ToolMessage:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_failure("file_info", read_error, code=code, data={"path": path}, runtime=runtime)

    ctx = get_backend_path_context(task_id)
    resolved_path = resolve_path_for_policy(path, task_id)
    if ctx.env_type != "local":
        return _file_info_backend(path, resolved_path, task_id=task_id, runtime=runtime)

    try:
        target = safe_path(path)
        if not target.exists():
            return tool_failure(
                "file_info",
                f"Path not found: {path}",
                code="not_found",
                data={"path": path},
                runtime=runtime,
            )
        return tool_success(
            "file_info",
            data=path_info(target),
            message="Path inspected.",
            runtime=runtime,
            content=f"Inspected {path}.",
        )
    except Exception as exc:
        return tool_failure("file_info", str(exc), runtime=runtime)


@tool("file_info", args_schema=FileInfoInput)
def file_info(path: str, runtime: ToolRuntime) -> ToolMessage:
    """Return metadata for a workspace file or directory."""
    return _file_info_impl(path=path, runtime=runtime)
