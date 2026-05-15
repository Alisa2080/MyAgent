import json
import os
from pathlib import Path

from agent_tools.hermes_terminal_toolkit.terminal import run_terminal


def _normalize_task_id(task_id: str | None) -> str:
    return str(task_id).strip() if task_id and str(task_id).strip() else "default"


def run_foreground_command(
    command: str,
    *,
    workdir: str,
    timeout: int = 120,
    task_id: str = "default",
) -> dict:
    env_type = os.getenv("TERMINAL_ENV", "local").strip().lower() or "local"
    if env_type != "local":
        return {
            "output": "",
            "exit_code": -1,
            "error": (
                "execute_command currently supports only the local Hermes backend. "
                f"Found TERMINAL_ENV={env_type!r}."
            ),
            "status": "error",
        }

    raw = run_terminal(
        command=command,
        background=False,
        timeout=timeout,
        task_id=_normalize_task_id(task_id),
        workdir=str(Path(workdir).resolve()),
        pty=False,
        force=True,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "output": "",
            "exit_code": -1,
            "error": "Hermes terminal returned invalid JSON.",
            "status": "error",
            "raw": raw,
        }

    if not isinstance(payload, dict):
        return {
            "output": "",
            "exit_code": -1,
            "error": f"Hermes terminal returned unexpected payload type: {type(payload).__name__}",
            "status": "error",
            "raw": raw,
        }
    return payload
