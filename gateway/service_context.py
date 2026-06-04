from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from gateway.service_env import get_service_env_file


@dataclass(frozen=True)
class GatewayServiceRuntimeContext:
    project_root: Path
    python_executable: str
    service_env_file: Path
    pythonpath: str = ""


def detect_project_root(start: str | Path | None = None) -> Path:
    if start is None:
        start_path = Path(__file__).resolve()
    else:
        start_path = Path(start).resolve()
    current = start_path.parent if start_path.is_file() else start_path
    for candidate in (current, *current.parents):
        if (candidate / "agent_cli").exists() and (candidate / "gateway").exists():
            return candidate
    return Path.cwd().resolve()


def build_service_runtime_context(
    *,
    project_root: str | Path | None = None,
    module_path: str | Path | None = None,
) -> GatewayServiceRuntimeContext:
    root = Path(project_root).resolve() if project_root is not None else detect_project_root(module_path)
    return GatewayServiceRuntimeContext(
        project_root=root,
        python_executable=sys.executable,
        service_env_file=get_service_env_file(),
        pythonpath=str(root),
    )
