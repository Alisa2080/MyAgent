from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
    TodoListMiddleware,
    ModelRetryMiddleware,
    ToolRetryMiddleware,
    ModelCallLimitMiddleware
)

from agent_core.delegation import BASE_TOOLS, task
from agent_core.human_loop import FlexibleHumanInTheLoopMiddleware
from agent_core.memory import memory_store
from agent_core.model_config import MAIN_MODEL, SMALL_MODEL
from agent_core.system_prompt import (
    SystemPromptBuilder,
    build_prompt_context,
    load_project_instruction_blocks,
    model_display_name,
)
from agent_core.terminal_lifecycle import recover_terminal_processes
from agent_core.tool_limits import build_tool_call_limit_middleware
from agent_core.workspace import WORKDIR
from agent_tools.memory_tools import memory_manage


TODO_SYSTEM_PROMPT = (
    "Use the write_todos tool to track complex, multi-step work. "
    "Create todos only when they materially improve coordination or progress visibility. "
    "Keep items concrete, update statuses as work progresses, and mark completed items immediately."
)

TODO_TOOL_DESCRIPTION = (
    "Create or update a concise task list for complex work. "
    "Use pending, in_progress, and completed statuses to reflect the current plan."
)

HUMAN_INTERRUPT_ON = {
    "terminal": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this Hermes terminal command before it executes.",
    },
    "process": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this Hermes background process action before it executes.",
    },
    "write_file": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this file write before it modifies the workspace.",
    },
    "patch": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this file patch before it modifies the workspace. Patches may add, update, move, or delete files.",
    },
    "memory_manage": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this durable memory change before it is written to disk.",
    },
    "skill_manage": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this procedural skill change before it is written to disk.",
    },
}


def build_agent():
    memory_store.load_from_disk()
    recover_terminal_processes()
    memory_blocks = []
    mem_block = memory_store.format_for_system_prompt("memory")
    user_block = memory_store.format_for_system_prompt("user")

    if mem_block:
        memory_blocks.append(mem_block)
    if user_block:
        memory_blocks.append(user_block)

    prompt_context = build_prompt_context(
        workdir=WORKDIR,
        model_name=model_display_name(MAIN_MODEL),
        memory_blocks=memory_blocks,
        project_instruction_blocks=load_project_instruction_blocks(WORKDIR),
    )

    return create_agent(
        model=MAIN_MODEL,
        system_prompt=SystemPromptBuilder().build_parent(prompt_context),
        middleware=[
            SummarizationMiddleware(
                model=SMALL_MODEL,
                trigger=[("messages", 50), ("tokens", 30000)],
                keep=("messages", 20),
                trim_tokens_to_summarize=12000,
            ),
            TodoListMiddleware(
                system_prompt=TODO_SYSTEM_PROMPT,
                tool_description=TODO_TOOL_DESCRIPTION,
            ),
            *build_tool_call_limit_middleware(include_task=True),
            FlexibleHumanInTheLoopMiddleware(
                interrupt_on=HUMAN_INTERRUPT_ON,
                description_prefix="Approval required before tool execution",
            ),
            # ModelCallLimitMiddleware(
            #     thread_limit=20,
            #     run_limit=100,
            #     exit_behavior="end",
            # ),
            ToolRetryMiddleware(max_retries=3),
            ModelRetryMiddleware(max_retries=2),
        ],
        tools=[*BASE_TOOLS, memory_manage, task],
    )
