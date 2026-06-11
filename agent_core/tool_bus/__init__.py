"""Tool dispatch middleware helpers."""

from agent_core.tool_bus.models import (
    PostToolHook,
    PreToolHook,
    ToolBusHooks,
    ToolBusRequest,
    ToolBusResult,
    ToolResponse,
    TransformToolResultHook,
)


def __getattr__(name: str):
    if name == "ToolBusMiddleware":
        from agent_core.tool_bus_middleware import ToolBusMiddleware

        return ToolBusMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PostToolHook",
    "PreToolHook",
    "ToolBusHooks",
    "ToolBusMiddleware",
    "ToolBusRequest",
    "ToolBusResult",
    "ToolResponse",
    "TransformToolResultHook",
]
