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
    from agent_tools.public.web import web_fetch as public_web_fetch
    from agent_tools.public.web import web_search as public_web_search
    from agent_tools.skill_manage import skill_manage
    from agent_tools.skills import skill_view, skills_list
    from agent_tools.web import web_fetch, web_search

    assert public_memory_manage is memory_manage
    assert public_web_search is web_search
    assert public_web_fetch is web_fetch
    assert public_skills_list is skills_list
    assert public_skill_view is skill_view
    assert public_skill_manage is skill_manage
