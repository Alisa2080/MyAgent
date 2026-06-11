import logging

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage

from agent_core.message_utils import extract_text_from_agent_response
from agent_core.model_config import SMALL_MODEL
from agent_core.schemas import TaskInput
from agent_core.system_prompt import (
    SystemPromptBuilder,
    build_prompt_context,
    load_project_instruction_blocks,
    model_display_name,
)
from agent_core.tool_limits import build_tool_call_limit_middleware
from agent_core.workspace import WORKDIR
from agent_tools.public.clarify import clarify
from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_extract, web_search
from agent_tools.shared.tool_result import tool_failure, tool_success

logger = logging.getLogger(__name__)
_TASK_RECURSION_LIMIT = 90

READ_ONLY_TOOLS = [
    list_directory,
    search_files,
    read_file,
    file_info,
    web_search,
    web_extract,
    skills_list,
    skill_view,
]

BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    write_file,
    patch,
    terminal,
    process,
    skill_manage,
    clarify,
]


def build_task_subagent():
    prompt_context = build_prompt_context(
        workdir=WORKDIR,
        model_name=model_display_name(SMALL_MODEL),
        project_instruction_blocks=load_project_instruction_blocks(WORKDIR),
    )
    return create_agent(
        model=SMALL_MODEL,
        system_prompt=(
            SystemPromptBuilder().build_subagent(prompt_context)
            + "\n\nWhen enough evidence is gathered, stop exploring and return a concise plain-text summary immediately. "
            + "Avoid repeated searches or re-reading the same source unless it yields materially new information."
        ),
        middleware=build_tool_call_limit_middleware(),
        tools=READ_ONLY_TOOLS,
    )


@tool("task", args_schema=TaskInput)
def task(
    prompt: str,
    description: str = "subtask",
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
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
            {"recursion_limit": _TASK_RECURSION_LIMIT},
        )
        summary = extract_text_from_agent_response(response)
        if not summary:
            logger.warning("task: delegated subagent task=%s returned no final text", description)
            return tool_success(
                "task",
                message="Subagent completed with no summary.",
                data={"description": description, "summary": "(no summary)"},
                content="Subagent completed with no summary.",
                runtime=runtime,
            )
        logger.info("task: completed delegated subagent task=%s", description)
        return tool_success(
            "task",
            message="Subagent task completed.",
            data={"description": description, "summary": summary},
            content=f"Subagent task completed: {description}.",
            runtime=runtime,
        )
    except Exception as exc:
        logger.exception("task: delegated subagent task=%s failed: %s", description, exc)
        return tool_failure(
            "task",
            f"Subagent task failed ({description}): {exc}",
            code="subagent_failed",
            data={"description": description},
            runtime=runtime,
        )
