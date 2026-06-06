from langchain.agents.middleware import ToolCallLimitMiddleware


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
