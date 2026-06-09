from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from agent_tools.public.code_execution import execute_code
from agent_tools.public.memory import memory_manage


RiskLevel = Literal["low", "medium", "high"]
CHECK_FN_TTL_SECONDS = 30.0

_check_cache: dict[tuple[str, str | None], tuple[float, bool]] = {}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    toolset: str
    tool: Any
    check_fn: Callable[[], bool] | None = None
    max_result_size_chars: int | None = None
    read_only: bool = False
    risk_level: RiskLevel = "low"
    emoji: str = ""
    enabled_by_default: bool = True


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def clear_tool_catalog_cache() -> None:
    _check_cache.clear()


def _passes_check(spec: ToolSpec, *, runtime_profile: str | None = None) -> bool:
    if spec.check_fn is None:
        return True
    key = (spec.name, runtime_profile)
    now = time.monotonic()
    cached = _check_cache.get(key)
    if cached is not None:
        checked_at, value = cached
        if now - checked_at < CHECK_FN_TTL_SECONDS:
            return value
    try:
        value = bool(spec.check_fn())
    except Exception:
        value = False
    _check_cache[key] = (now, value)
    return value


def _spec(
    tool: Any,
    *,
    toolset: str,
    read_only: bool,
    risk_level: RiskLevel = "low",
    emoji: str = "",
    max_result_size_chars: int | None = None,
    enabled_by_default: bool = True,
    check_fn: Callable[[], bool] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=_tool_name(tool),
        toolset=toolset,
        tool=tool,
        check_fn=check_fn,
        max_result_size_chars=max_result_size_chars,
        read_only=read_only,
        risk_level=risk_level,
        emoji=emoji,
        enabled_by_default=enabled_by_default,
    )


def _code_execution_available_by_default() -> bool:
    return True


def _is_code_execution_default_allowed(runtime_profile: str | None) -> bool:
    profile = (runtime_profile or "").strip().lower()
    return profile in {"", "dev", "test"}


def default_tool_specs(*, include_cron_tools: bool = False) -> list[ToolSpec]:
    from agent_tools.public.files import (
        file_info,
        list_directory,
        patch,
        read_file,
        search_files,
        write_file,
    )
    from agent_tools.public.skills import skill_manage, skill_view, skills_list
    from agent_tools.public.terminal import process, terminal
    from agent_tools.public.web import web_extract, web_search
    from agent_tools.public.clarify import clarify
    from agent_core.delegation import task

    specs = [
        _spec(list_directory, toolset="file_read", read_only=True),
        _spec(search_files, toolset="file_read", read_only=True),
        _spec(read_file, toolset="file_read", read_only=True),
        _spec(file_info, toolset="file_read", read_only=True),
        _spec(web_search, toolset="web", read_only=True),
        _spec(web_extract, toolset="web", read_only=True),
        _spec(skills_list, toolset="skills", read_only=True),
        _spec(skill_view, toolset="skills", read_only=True),
        _spec(write_file, toolset="file_write", read_only=False, risk_level="medium"),
        _spec(patch, toolset="file_write", read_only=False, risk_level="medium"),
        _spec(terminal, toolset="terminal", read_only=False, risk_level="high", max_result_size_chars=100_000),
        _spec(process, toolset="terminal", read_only=False, risk_level="high", max_result_size_chars=100_000),
        _spec(
            execute_code,
            toolset="code_execution",
            read_only=False,
            risk_level="high",
            max_result_size_chars=100_000,
            enabled_by_default=True,
            check_fn=_code_execution_available_by_default,
        ),
        _spec(skill_manage, toolset="skills", read_only=False, risk_level="medium"),
        _spec(clarify, toolset="clarify", read_only=True, emoji="?"),
        _spec(memory_manage, toolset="memory", read_only=False, risk_level="medium"),
        _spec(task, toolset="delegation", read_only=True),
    ]
    if include_cron_tools:
        from agent_tools.public.cronjob import cronjob

        specs.append(_spec(cronjob, toolset="cron", read_only=False, risk_level="medium"))
    return specs


def get_tool_specs(*, include_cron_tools: bool = False) -> list[ToolSpec]:
    return default_tool_specs(include_cron_tools=include_cron_tools)


def get_tool_spec(name: str, *, include_cron_tools: bool = True) -> ToolSpec | None:
    for spec in get_tool_specs(include_cron_tools=include_cron_tools):
        if spec.name == name:
            return spec
    return None


def build_tools_from_specs(
    specs: Iterable[ToolSpec],
    enabled_toolsets: list[str] | None = None,
    *,
    runtime_profile: str | None = None,
) -> list[Any]:
    enabled = set(enabled_toolsets) if enabled_toolsets is not None else None
    tools = []
    for spec in specs:
        if enabled is None and not spec.enabled_by_default:
            continue
        if enabled is not None and spec.toolset not in enabled:
            continue
        if spec.toolset == "code_execution" and enabled is None:
            if not _is_code_execution_default_allowed(runtime_profile):
                continue
        if not _passes_check(spec, runtime_profile=runtime_profile):
            continue
        tools.append(spec.tool)
    return tools


def build_tools(
    enabled_toolsets: list[str] | None = None,
    *,
    include_cron_tools: bool = False,
    runtime_profile: str | None = None,
) -> list[Any]:
    return build_tools_from_specs(
        default_tool_specs(include_cron_tools=include_cron_tools),
        enabled_toolsets,
        runtime_profile=runtime_profile,
    )
