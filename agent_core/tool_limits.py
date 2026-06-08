from __future__ import annotations

from typing import Annotated
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallLimitMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.channels.untracked_value import UntrackedValue
from langchain.agents.middleware.types import AgentState, PrivateStateAttr

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
    consecutive_read_only_tool_count: Annotated[int, UntrackedValue, PrivateStateAttr]


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

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        blocked = self._before_tool_call(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        blocked = self._before_tool_call(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    def _before_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = _request_tool_name(request)
        state = getattr(request, "state", None)
        if not isinstance(state, dict):
            state = {}
        if self._is_read_only(tool_name):
            count = int(state.get(READ_ONLY_COUNT_STATE_KEY, 0) or 0) + 1
            state[READ_ONLY_COUNT_STATE_KEY] = count
            if count > self.max_consecutive_read_only:
                return _read_loop_failure(request, count, self.max_consecutive_read_only)
            return None
        state[READ_ONLY_COUNT_STATE_KEY] = 0
        return None

    def _is_read_only(self, tool_name: str) -> bool:
        spec = self.specs.get(tool_name)
        if spec is None:
            return self.include_unknown_as_read_only
        return bool(spec.read_only)


def _request_tool_name(request: ToolCallRequest) -> str:
    tool_call = getattr(request, "tool_call", None) or {}
    if isinstance(tool_call, dict) and tool_call.get("name"):
        return str(tool_call["name"])
    tool = getattr(request, "tool", None)
    return str(getattr(tool, "name", "") or "")


def _read_loop_failure(request: ToolCallRequest, count: int, limit: int) -> ToolMessage:
    tool_name = _request_tool_name(request)
    runtime = getattr(request, "runtime", None)
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
