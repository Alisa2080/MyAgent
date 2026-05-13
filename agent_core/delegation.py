import logging

from langchain.agents import create_agent
from langchain.tools import tool

from agent_core.message_utils import extract_text_from_agent_response
from agent_core.model_config import SMALL_MODEL
from agent_core.prompts import SUBAGENT_SYSTEM_PROMPT
from agent_core.schemas import TaskInput
from agent_core.tool_limits import build_tool_call_limit_middleware
from agent_tools.file_tools import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.shell import execute_command
from agent_tools.skill_manage import skill_manage
from agent_tools.skills import skill_view, skills_list
from agent_tools.web import web_fetch, web_search

logger = logging.getLogger(__name__)

READ_ONLY_TOOLS = [
    list_directory,
    search_files,
    read_file,
    file_info,
    web_search,
    web_fetch,
    skills_list,
    skill_view,
]

BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    write_file,
    patch,
    execute_command,
    skill_manage,
]


def build_task_subagent():
    return create_agent(
        model=SMALL_MODEL,
        system_prompt=SUBAGENT_SYSTEM_PROMPT,
        middleware=build_tool_call_limit_middleware(),
        tools=READ_ONLY_TOOLS,
    )


@tool("task", args_schema=TaskInput)
def task(prompt: str, description: str = "subtask") -> str:
    """Spawn a fresh-context read-only subagent for analysis, exploration, or review."""
    logger.info("task: starting delegated subagent task=%s", description)
    try:
        response = build_task_subagent().invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Task description: {description}\n\n"
                            f"{prompt}\n\n"
                            "You are read-only: do not attempt to edit files, delete files, "
                            "or run shell commands. Return a plain-text summary only after completing the task."
                        ),
                    }
                ]
            },
            {"recursion_limit": 40},
        )
        summary = extract_text_from_agent_response(response)
        if not summary:
            logger.warning("task: delegated subagent task=%s returned no final text", description)
            return "(no summary)"
        logger.info("task: completed delegated subagent task=%s", description)
        return summary
    except Exception as exc:
        logger.exception("task: delegated subagent task=%s failed: %s", description, exc)
        return f"Subagent task failed ({description}): {exc}"
