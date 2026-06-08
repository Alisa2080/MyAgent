import importlib
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_httpx_for_web_imports(monkeypatch):
    if "httpx" not in sys.modules:
        monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace())


def test_public_files_exports_existing_tool_objects():
    from agent_tools.file_tools import file_info, list_directory, patch, read_file, search_files, write_file
    from agent_tools.public.files import (
        file_info as public_file_info,
        list_directory as public_list_directory,
        patch as public_patch,
        read_file as public_read_file,
        search_files as public_search_files,
        write_file as public_write_file,
    )

    assert public_list_directory is list_directory
    assert public_read_file is read_file
    assert public_write_file is write_file
    assert public_patch is patch
    assert public_search_files is search_files
    assert public_file_info is file_info


def test_public_terminal_exports_existing_tool_objects():
    from agent_tools.public.terminal import process as public_process
    from agent_tools.public.terminal import terminal as public_terminal
    from agent_tools.terminal_tools import process, terminal

    assert public_terminal is terminal
    assert public_process is process


def test_public_web_memory_and_skills_exports_existing_tool_objects():
    from agent_tools.memory_tools import memory_manage
    from agent_tools.public.memory import memory_manage as public_memory_manage
    from agent_tools.public.skills import skill_manage as public_skill_manage
    from agent_tools.public.skills import skill_view as public_skill_view
    from agent_tools.public.skills import skills_list as public_skills_list
    from agent_tools.public.web import web_extract as public_web_extract
    from agent_tools.public.web import web_search as public_web_search
    from agent_tools.skill_manage import skill_manage
    from agent_tools.skills import skill_view, skills_list
    from agent_tools.web import web_extract, web_search

    assert public_memory_manage is memory_manage
    assert public_web_search is web_search
    assert public_web_extract is web_extract
    assert public_skills_list is skills_list
    assert public_skill_view is skill_view
    assert public_skill_manage is skill_manage


def test_web_fetch_removed_from_public_surfaces():
    import agent_tools.public as public
    import agent_tools.public.web as public_web

    assert "web_fetch" not in public.__all__
    assert not hasattr(public_web, "web_fetch")
    assert not hasattr(public, "web_fetch")


def test_shared_common_exports_existing_helpers():
    from agent_tools.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path, truncate
    from agent_tools.shared.common import (
        DEFAULT_EXCLUDE_DIRS as public_default_exclude_dirs,
        path_info as public_path_info,
        relative_path as public_relative_path,
        truncate as public_truncate,
    )

    assert public_default_exclude_dirs is DEFAULT_EXCLUDE_DIRS
    assert public_path_info is path_info
    assert public_relative_path is relative_path
    assert public_truncate is truncate


def test_shared_policy_and_output_exports_existing_helpers():
    from agent_tools.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path
    from agent_tools.shared.file_policy import (
        ensure_patch_paths as public_ensure_patch_paths,
        ensure_read_allowed as public_ensure_read_allowed,
        ensure_workspace_path as public_ensure_workspace_path,
    )
    from agent_tools.shared.tool_output import tool_error as public_tool_error
    from agent_tools.shared.tool_output import tool_ok as public_tool_ok
    from agent_tools.tool_output import tool_error, tool_ok

    assert public_ensure_patch_paths is ensure_patch_paths
    assert public_ensure_read_allowed is ensure_read_allowed
    assert public_ensure_workspace_path is ensure_workspace_path
    assert public_tool_error is tool_error
    assert public_tool_ok is tool_ok


def test_shared_tool_result_exports_existing_helpers():
    from agent_tools.shared.tool_result import tool_failure as public_tool_failure
    from agent_tools.shared.tool_result import tool_success as public_tool_success
    from agent_tools.tool_result import tool_failure, tool_success

    assert public_tool_success is tool_success
    assert public_tool_failure is tool_failure


def test_agent_core_uses_public_tool_facades_for_runtime_registration():
    from pathlib import Path

    delegation_source = Path("agent_core/delegation.py").read_text()
    builders_source = Path("agent_core/builders.py").read_text()
    catalog_source = Path("agent_core/tool_catalog.py").read_text()
    system_prompt_source = Path("agent_core/system_prompt.py").read_text()

    assert "from agent_tools.public.files import" in delegation_source
    assert "from agent_tools.public.terminal import" in delegation_source
    assert "from agent_tools.public.web import" in delegation_source
    assert "from agent_tools.public.skills import" in delegation_source
    assert "from agent_tools.public.memory import" in catalog_source
    assert "from agent_tools.public.skills import build_skills_system_prompt" in system_prompt_source


def test_public_package_exports_clarify():
    from agent_tools.public import __all__, clarify
    assert "clarify" in __all__
    assert getattr(clarify, "name", None) == "clarify"


def test_public_web_surface_replaces_web_fetch_with_web_extract():
    import agent_tools.public as public
    import agent_tools.public.web as public_web

    assert "web_extract" in public.__all__
    assert "web_fetch" not in public.__all__
    assert hasattr(public, "web_extract")
    assert hasattr(public_web, "web_extract")
    assert not hasattr(public, "web_fetch")
    assert not hasattr(public_web, "web_fetch")


def test_public_package_does_not_eagerly_import_cronjob(monkeypatch):
    for module_name in [
        "agent_tools.public",
        "agent_tools.public.cronjob",
        "cron.jobs",
    ]:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    public = importlib.import_module("agent_tools.public")

    assert "agent_tools.public.cronjob" not in sys.modules
    assert "cron.jobs" not in sys.modules

    from agent_tools.public import memory_manage

    assert getattr(memory_manage, "name", None) == "memory_manage"
    assert "agent_tools.public.cronjob" not in sys.modules
    assert "cron.jobs" not in sys.modules

    cronjob = public.cronjob

    assert getattr(cronjob, "name", None) == "cronjob"
    assert "agent_tools.public.cronjob" in sys.modules
    assert "cron.jobs" in sys.modules
