from langchain.agents.middleware import ToolCallLimitMiddleware


WEB_SEARCH_RUN_LIMIT = 3
WEB_SEARCH_THREAD_LIMIT = 8
WEB_FETCH_RUN_LIMIT = 5
WEB_FETCH_THREAD_LIMIT = 12
SEARCH_FILES_RUN_LIMIT = 8
SEARCH_FILES_THREAD_LIMIT = 20
EXECUTE_COMMAND_RUN_LIMIT = 8
EXECUTE_COMMAND_THREAD_LIMIT = 16
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
            tool_name="web_fetch",
            run_limit=WEB_FETCH_RUN_LIMIT,
            thread_limit=WEB_FETCH_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="search_files",
            run_limit=SEARCH_FILES_RUN_LIMIT,
            thread_limit=SEARCH_FILES_THREAD_LIMIT,
        ),
        ToolCallLimitMiddleware(
            tool_name="execute_command",
            run_limit=EXECUTE_COMMAND_RUN_LIMIT,
            thread_limit=EXECUTE_COMMAND_THREAD_LIMIT,
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
