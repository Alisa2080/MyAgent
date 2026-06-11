"""Compatibility facade for tool argument normalization."""

from agent_core.tool_args.coercion import (
    ToolArgCoercionError,
    normalize_tool_args,
)

__all__ = ["ToolArgCoercionError", "normalize_tool_args"]
