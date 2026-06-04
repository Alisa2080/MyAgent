from __future__ import annotations

from gateway.service_context import GatewayServiceRuntimeContext

UNIT_NAME = "agent-gateway.service"


def _quote_systemd(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _quote_directive_value(value: str) -> str:
    escaped = _quote_systemd(value)
    if not value or any(char.isspace() or char in {'"', "\\"} for char in value):
        return f'"{escaped}"'
    return escaped


def _quote_arg(value: str) -> str:
    escaped = _quote_systemd(value).replace("$", "$$")
    if not value or any(char.isspace() or char in {'"', "\\"} for char in value):
        return f'"{escaped}"'
    return escaped


def render_unit(context: GatewayServiceRuntimeContext, *, transport: str) -> str:
    command = " ".join(
        _quote_arg(part)
        for part in (
            context.python_executable,
            "-m",
            "agent_cli.main",
            "gateway",
            transport,
        )
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=Agent Gateway",
            "",
            "[Service]",
            f"WorkingDirectory={_quote_directive_value(str(context.project_root))}",
            f'Environment="PYTHONPATH={_quote_systemd(context.pythonpath or str(context.project_root))}"',
            f"EnvironmentFile=-{_quote_directive_value(str(context.service_env_file))}",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
