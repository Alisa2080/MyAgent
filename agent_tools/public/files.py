import json
import os
import re
from pathlib import Path
from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.session_context import hermes_task_id_from_runtime
from agent_core.workspace import WORKDIR, safe_path
from agent_tools.file_toolkit.file_tools import (
    _resolve_path_for_task,
    patch_tool,
    read_file_tool,
    search_tool,
    write_file_tool,
)
from agent_tools.file_toolkit.patch_parser import parse_v4a_patch
from agent_tools.shared.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path
from agent_tools.shared.file_policy import ensure_workspace_path
from agent_tools.shared.tool_output import tool_error, tool_ok


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


def _extract_meta(payload: dict, *keys: str) -> dict:
    meta: dict = {}

    warning = payload.pop("_warning", None)
    if warning:
        meta["warnings"] = warning if isinstance(warning, list) else [warning]

    hint = payload.pop("_hint", None)
    if hint:
        meta["hint"] = hint

    for key in keys:
        if key in payload:
            meta[key] = payload.pop(key)

    return meta


def _wrap_file_tool_result(
    tool_name: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return tool_error(
            tool_name,
            "Tool returned invalid JSON.",
            code="invalid_response",
            meta={"raw": raw},
        )

    message = payload.pop("message", success_message)
    payload.pop("status", None)
    payload.pop("success", None)
    meta = _extract_meta(payload, *meta_keys)
    error_message = payload.pop("error", None)

    if error_message:
        return tool_error(
            tool_name,
            str(error_message),
            code="tool_error",
            data=payload or None,
            meta=meta,
        )

    return tool_ok(tool_name, data=payload or None, message=message, meta=meta)


def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return hermes_task_id_from_runtime(runtime)


def _allowed_workspace_roots_for_task(task_id: str) -> list[Path]:
    roots = [WORKDIR.resolve()]
    try:
        from agent_tools.hermes_terminal_toolkit import terminal_tool

        config = terminal_tool._get_env_config()
    except Exception:
        config = {}

    env_type = config.get("env_type", "local")
    if env_type != "local":
        cwd = config.get("cwd")
        if cwd:
            expanded = Path(os.path.expanduser(str(cwd)))
            if expanded.is_absolute():
                roots.append(expanded.resolve())
        if env_type in ("docker", "singularity"):
            roots.append(Path("/workspace"))

    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root not in seen:
            deduped.append(root)
            seen.add(root)
    return deduped


def _is_under_allowed_workspace_root(resolved: Path, task_id: str) -> bool:
    return any(resolved.is_relative_to(root) for root in _allowed_workspace_roots_for_task(task_id))


def _blocked_read_dirs_for_task(task_id: str) -> list[Path]:
    blocked_dirs: list[Path] = []
    seen: set[Path] = set()
    for root in _allowed_workspace_roots_for_task(task_id):
        for blocked_dir in (
            root / "skills" / ".hub",
            root / "skills" / ".hub" / "index-cache",
        ):
            resolved = blocked_dir.resolve()
            if resolved not in seen:
                blocked_dirs.append(resolved)
                seen.add(resolved)
    return blocked_dirs


def _ensure_workspace_path_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = Path(_resolve_path_for_task(path or ".", task_id))
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"
    return None


def _ensure_read_allowed_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = Path(_resolve_path_for_task(path or ".", task_id))
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"

    for blocked_dir in _blocked_read_dirs_for_task(task_id):
        try:
            resolved.relative_to(blocked_dir)
        except ValueError:
            continue
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


@tool("list_directory", args_schema=ListDirectoryInput)
def list_directory(path: str = ".", recursive: bool = False, include_hidden: bool = False, limit: int = 200) -> str:
    """List files and directories inside the workspace."""
    path_error = ensure_workspace_path(path)
    if path_error:
        return tool_error("list_directory", path_error, code="invalid_path")
    try:
        dir_path = safe_path(path)
        if not dir_path.exists():
            return tool_error("list_directory", f"Directory not found: {path}", code="not_found")
        if not dir_path.is_dir():
            return tool_error("list_directory", f"Not a directory: {path}", code="not_directory")

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

        return tool_ok(
            "list_directory",
            data={"path": path, "entries": items, "total": total},
            message="Directory listed.",
            meta={"truncated": total > len(items)},
        )
    except Exception as exc:
        return tool_error("list_directory", str(exc))


def _read_file_impl(path: str, offset: int = 1, limit: int = 500, runtime: ToolRuntime | None = None) -> str:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_error("read_file", read_error, code=code)
    raw = read_file_tool(path=path, offset=offset, limit=limit, task_id=task_id)
    return _wrap_file_tool_result(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
    )


@tool("read_file", args_schema=ReadFileInput)
def read_file(path: str, runtime: ToolRuntime, offset: int = 1, limit: int = 500) -> str:
    """Read a text file with line numbers and pagination."""
    return _read_file_impl(path=path, offset=offset, limit=limit, runtime=runtime)


def _write_file_impl(path: str, content: str, runtime: ToolRuntime | None = None) -> str:
    task_id = _task_id_from_runtime(runtime)
    path_error = _ensure_workspace_path_for_task(path, task_id)
    if path_error:
        return tool_error("write_file", path_error, code="invalid_path")
    raw = write_file_tool(path=path, content=content, task_id=task_id)
    return _wrap_file_tool_result("write_file", raw, success_message="File written.")


@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str, runtime: ToolRuntime) -> str:
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
) -> str:
    task_id = _task_id_from_runtime(runtime)
    if mode == "replace":
        if not path:
            return tool_error("patch", "path is required for replace mode.", code="invalid_input")
        path_error = _ensure_workspace_path_for_task(path, task_id)
        if path_error:
            return tool_error("patch", path_error, code="invalid_path")
    if mode == "patch":
        path_error = _ensure_patch_paths_for_task(patch, task_id)
        if path_error:
            return tool_error("patch", path_error, code="invalid_path")
    raw = patch_tool(
        mode=mode,
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        patch=patch,
        task_id=task_id,
    )
    return _wrap_file_tool_result("patch", raw, success_message="Patch applied.")


@tool("patch", args_schema=PatchInput)
def patch(
    runtime: ToolRuntime,
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
) -> str:
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
) -> str:
    task_id = _task_id_from_runtime(runtime)
    read_error = _ensure_read_allowed_for_task(path, task_id)
    if read_error:
        code = "access_denied" if read_error.startswith("Access denied:") else "invalid_path"
        return tool_error("search_files", read_error, code=code)
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
    return _wrap_file_tool_result(
        "search_files",
        raw,
        success_message="Search completed.",
        meta_keys=("truncated",),
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
) -> str:
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


@tool("file_info", args_schema=FileInfoInput)
def file_info(path: str) -> str:
    """Return metadata for a workspace file or directory."""
    path_error = ensure_workspace_path(path)
    if path_error:
        return tool_error("file_info", path_error, code="invalid_path", data={"path": path})
    try:
        target = safe_path(path)
        if not target.exists():
            return tool_error("file_info", f"Path not found: {path}", code="not_found", data={"path": path})
        return tool_ok("file_info", data=path_info(target), message="Path inspected.")
    except Exception as exc:
        return tool_error("file_info", str(exc))
