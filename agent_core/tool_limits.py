from __future__ import annotations

from typing import Annotated
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.channels.untracked_value import UntrackedValue
from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from typing_extensions import NotRequired

from agent_core.tool_catalog import ToolSpec
from agent_tools.shared.tool_result import tool_failure


DEFAULT_CONSECUTIVE_READ_ONLY_LIMIT = 10
READ_LOOP_LIMIT_ERROR_CODE = "read_loop_limit"
READ_ONLY_COUNT_STATE_KEY = "consecutive_read_only_tool_count"


WEB_SEARCH_RUN_LIMIT = 3
WEB_SEARCH_THREAD_LIMIT = 8
WEB_EXTRACT_RUN_LIMIT = 5
WEB_EXTRACT_THREAD_LIMIT = 12
SEARCH_FILES_RUN_LIMIT = 8
SEARCH_FILES_THREAD_LIMIT = 20
TERMINAL_RUN_LIMIT = 8
TERMINAL_THREAD_LIMIT = 16
PROCESS_RUN_LIMIT = 12
PROCESS_THREAD_LIMIT = 32
TASK_RUN_LIMIT = 5
TASK_THREAD_LIMIT = 10
EXECUTE_CODE_RUN_LIMIT = 4
EXECUTE_CODE_THREAD_LIMIT = 8


def build_tool_call_limit_middleware(
    *,
    include_task: bool = False,
) -> list[ToolCallLimitMiddleware]:
    """Return tool-specific call limits for expensive or recursive tools."""
    middleware = [
        ToolCallLimitMiddleware(
            tool_name="web_search",
            run_limit=WEB_SEARCH_RUN_LIMIT,
            thread_limit=WEB_SEARCH_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="web_extract",
            run_limit=WEB_EXTRACT_RUN_LIMIT,
            thread_limit=WEB_EXTRACT_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="search_files",
            run_limit=SEARCH_FILES_RUN_LIMIT,
            thread_limit=SEARCH_FILES_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="terminal",
            run_limit=TERMINAL_RUN_LIMIT,
            thread_limit=TERMINAL_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="process",
            run_limit=PROCESS_RUN_LIMIT,
            thread_limit=PROCESS_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="execute_code",
            run_limit=EXECUTE_CODE_RUN_LIMIT,
            thread_limit=EXECUTE_CODE_THREAD_LIMIT,
        ),
    ]
    if include_task:
        middleware.append(
            ToolCallLimitMiddleware(
                tool_name="task",
                run_limit=TASK_RUN_LIMIT,
                thread_limit=TASK_THREAD_LIMIT,
            )
        )
    return middleware


class ConsecutiveReadOnlyToolLimitState(AgentState):
    consecutive_read_only_tool_count: NotRequired[
        Annotated[int, UntrackedValue, PrivateStateAttr]
    ]


class ConsecutiveReadOnlyToolLimitMiddleware(AgentMiddleware):
    state_schema = ConsecutiveReadOnlyToolLimitState

    def __init__(
        self,
        *,
        specs: Mapping[str, ToolSpec],
        max_consecutive_read_only: int = DEFAULT_CONSECUTIVE_READ_ONLY_LIMIT,
        include_unknown_as_read_only: bool = False,
    ) -> None:
        super().__init__()
        self.specs = dict(specs)
        self.max_consecutive_read_only = int(max_consecutive_read_only)
        self.include_unknown_as_read_only = include_unknown_as_read_only

    def after_model(
        self,
        state: ConsecutiveReadOnlyToolLimitState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        last_ai_message = _last_ai_message(messages)
        if last_ai_message is None or not last_ai_message.tool_calls:
            return None

        count = int(state.get(READ_ONLY_COUNT_STATE_KEY, 0) or 0)
        blocked_messages: list[ToolMessage] = []

        for tool_call in last_ai_message.tool_calls:
            tool_name = str(tool_call.get("name") or "")
            if self._is_read_only(tool_name):
                count += 1
                if count > self.max_consecutive_read_only:
                    blocked_messages.append(
                        _read_loop_failure(
                            tool_call,
                            count,
                            self.max_consecutive_read_only,
                        )
                    )
                continue
            count = 0

        result: dict[str, Any] = {READ_ONLY_COUNT_STATE_KEY: count}
        if blocked_messages:
            result["messages"] = blocked_messages
        return result

    async def aafter_model(
        self,
        state: ConsecutiveReadOnlyToolLimitState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    def _is_read_only(self, tool_name: str) -> bool:
        spec = self.specs.get(tool_name)
        if spec is None:
            return self.include_unknown_as_read_only
        return bool(spec.read_only)


def _last_ai_message(messages: list[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def _read_loop_failure(tool_call: dict[str, Any], count: int, limit: int) -> ToolMessage:
    tool_name = str(tool_call.get("name") or "")
    runtime = _RuntimeToolCallProxy(str(tool_call.get("id") or ""))
    return tool_failure(
        tool_name,
        (
            f"Blocked after {count} consecutive read-only tool calls. "
            f"The limit is {limit}. Stop repeating read-only tools, summarize the context already gathered, "
            "and switch strategy or take a non-read action."
        ),
        code=READ_LOOP_LIMIT_ERROR_CODE,
        runtime=runtime,
    )


class _RuntimeToolCallProxy:
    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = tool_call_id
