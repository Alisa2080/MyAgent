from __future__ import annotations

import plistlib

from gateway.service_context import GatewayServiceRuntimeContext

LABEL = "com.agent.gateway"


def render_plist(
    context: GatewayServiceRuntimeContext,
    *,
    transport: str,
    service_env: dict[str, str] | None = None,
) -> bytes:
    env = {"PYTHONPATH": context.pythonpath or str(context.project_root), **(service_env or {})}
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            context.python_executable,
            "-m",
            "agent_cli.main",
            "gateway",
            transport,
        ],
        "WorkingDirectory": str(context.project_root),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(context.project_root / ".agent-gateway.out.log"),
        "StandardErrorPath": str(context.project_root / ".agent-gateway.err.log"),
    }
    return plistlib.dumps(payload)
