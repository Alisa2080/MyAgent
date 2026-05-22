"""Preferred public import surface for LangChain-facing tools."""

import sys
import types

from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.memory import memory_manage
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_fetch, web_search


def __getattr__(name: str):
    if name == "cronjob":
        from agent_tools.public.cronjob import cronjob as cronjob_tool

        return cronjob_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _PublicToolsModule(types.ModuleType):
    def __getattribute__(self, name: str):
        value = super().__getattribute__(name)
        if name == "cronjob" and isinstance(value, types.ModuleType):
            return value.cronjob
        return value


sys.modules[__name__].__class__ = _PublicToolsModule


__all__ = [
    "file_info",
    "cronjob",
    "list_directory",
    "memory_manage",
    "patch",
    "process",
    "read_file",
    "search_files",
    "skill_manage",
    "skill_view",
    "skills_list",
    "terminal",
    "web_fetch",
    "web_search",
    "write_file",
]
