from __future__ import annotations

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.session_context import RuntimeContext
from agent_tools.file_toolkit.backend_paths import get_backend_path_context
from agent_tools.file_toolkit.redact import redact_sensitive_text
from agent_tools.shared.tool_result import tool_failure

from agent_tools.code_execution.config import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_STDERR_LIMIT_CHARS,
    DEFAULT_STDOUT_LIMIT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    CodeExecutionConfig,
)
from agent_tools.code_execution.dispatch import (
    CodeExecutionDispatcher,
    failure_payload,
    normalize_rpc_args,
    tool_message_to_rpc_payload,
)
from agent_tools.code_execution.local_rpc import CodeExecutionRpcServer
from agent_tools.code_execution.runners import (
    execute_code_with_backend,
    safe_child_env,
    sanitize_output,
)
from agent_tools.code_execution.stubs import (
    SANDBOX_ALLOWED_TOOLS,
    STAGE_ONE_ALLOWED_TOOLS,
    WEB_ALLOWED_TOOLS,
    build_execute_code_description,
    generate_file_rpc_tools_module,
    generate_hermes_tools_module,
    generate_uds_tools_module,
    visible_sandbox_tools,
)


class ExecuteCodeInput(BaseModel):
    code: str = Field(description="Python code to execute with constrained project tool access.")
    include_web: bool = Field(default=False, description="Expose web_search and web_extract in addition to local file development tools when the caller explicitly allows web access.")


def _default_public_execute_code_result(*, runtime: ToolRuntime) -> object:
    return tool_failure(
        "execute_code",
        "execute_code must be configured by tool_catalog before use so visible tool access matches the current runtime.",
        code="misconfigured_tool",
        runtime=runtime,
    )


@tool("execute_code", args_schema=ExecuteCodeInput)
def execute_code(code: str, runtime: ToolRuntime, include_web: bool = False) -> object:
    """Execute Python code in the active terminal backend with constrained tool access."""
    return _default_public_execute_code_result(runtime=runtime)


def resolve_visible_tools_for_profile(
    *,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    runtime_profile: str | None,
    include_web: bool,
) -> tuple[str, ...]:
    return visible_sandbox_tools(enabled_tools, include_web=include_web)


def execute_code_impl(
    *,
    code: str,
    runtime: object,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    include_web: bool,
    timeout_seconds: int | None = None,
    max_tool_calls: int | None = None,
    stdout_limit_chars: int | None = None,
    stderr_limit_chars: int | None = None,
) -> object:
    runtime_config = {}
    config_value = getattr(runtime, "config", None)
    if isinstance(config_value, dict):
        configurable = config_value.get("configurable")
        if isinstance(configurable, dict):
            candidate = configurable.get("code_execution")
            if isinstance(candidate, dict):
                runtime_config = dict(candidate)
    explicit = {
        key: value
        for key, value in {
            "timeout_seconds": timeout_seconds,
            "max_tool_calls": max_tool_calls,
            "stdout_limit_chars": stdout_limit_chars,
            "stderr_limit_chars": stderr_limit_chars,
        }.items()
        if value is not None
    }
    config = CodeExecutionConfig.from_sources({**runtime_config, **explicit})
    return execute_code_with_backend(
        code=code,
        runtime=runtime,
        enabled_tools=enabled_tools,
        include_web=include_web,
        config=config,
    )
