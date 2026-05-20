import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from agent_core.workspace import WORKDIR


@dataclass(frozen=True)
class BackendPathContext:
    env_type: str
    cwd: str
    configured_cwd: str | None = None
    host_cwd: str | None = None


def get_backend_path_context(task_id: str = "default") -> BackendPathContext:
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    config = terminal_tool._get_env_config()
    env_type = str(config.get("env_type") or "local")
    configured_cwd = config.get("cwd")
    host_cwd = config.get("host_cwd")

    active_env = terminal_tool.get_active_env(task_id or "default")
    env_type = str(getattr(active_env, "_hermes_env_type", env_type) or env_type)
    configured_cwd = _select_configured_cwd(
        env_type,
        config_value=configured_cwd,
        metadata_value=getattr(active_env, "_hermes_configured_cwd", None),
    )
    host_cwd = host_cwd or getattr(active_env, "_hermes_host_cwd", None)

    if env_type == "local":
        cwd = _resolve_local_cwd(getattr(active_env, "cwd", None))
    else:
        cwd = _normalize_backend_path(
            getattr(active_env, "cwd", None)
            or configured_cwd
            or _default_backend_cwd(env_type)
        )

    return BackendPathContext(
        env_type=env_type,
        cwd=cwd,
        configured_cwd=str(configured_cwd) if configured_cwd is not None else None,
        host_cwd=str(host_cwd) if host_cwd is not None else None,
    )


def allowed_workspace_roots_for_task(task_id: str = "default") -> list[str]:
    ctx = get_backend_path_context(task_id)
    return safe_write_roots_for_env(ctx, fallback_cwd=ctx.cwd)


def safe_write_roots_for_env(env, fallback_cwd=None) -> list[str]:
    env_type = str(
        getattr(env, "env_type", None)
        or getattr(env, "_hermes_env_type", None)
        or "local"
    )
    cwd = getattr(env, "cwd", None) or fallback_cwd
    configured_cwd = getattr(env, "configured_cwd", None) or getattr(
        env, "_hermes_configured_cwd", None
    )
    host_cwd = getattr(env, "host_cwd", None) or getattr(env, "_hermes_host_cwd", None)

    if env_type == "local":
        candidates = [
            WORKDIR,
            os.environ.get("TERMINAL_CWD"),
            cwd,
            configured_cwd,
            host_cwd,
        ]
        return _dedupe(_local_root(candidate) for candidate in candidates)

    candidates: list[str | Path | None] = [str(WORKDIR.resolve())]
    if env_type == "docker":
        candidates.append("/workspace")
    configured_root = _backend_configured_root(configured_cwd)
    candidates.extend([cwd, configured_root, host_cwd])
    return _dedupe(_backend_root(candidate) for candidate in candidates)


def resolve_path_for_policy(path, task_id: str = "default") -> str:
    ctx = get_backend_path_context(task_id)
    if ctx.env_type == "local":
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(ctx.cwd) / p
        return str(p.resolve())

    path_str = str(path)
    if posixpath.isabs(path_str):
        return _normalize_backend_path(path_str)
    return _normalize_backend_path(posixpath.join(ctx.cwd, path_str))


def path_is_under_any_root(path, roots) -> bool:
    candidate = _normalize_backend_path(str(path))
    for root in roots:
        normalized_root = _normalize_backend_path(str(root))
        try:
            if posixpath.commonpath([candidate, normalized_root]) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _resolve_local_cwd(cwd: str | None) -> str:
    base = cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()
    return str(Path(base).expanduser().resolve())


def _local_root(path) -> str | None:
    if not path:
        return None
    return str(Path(path).expanduser().resolve())


def _backend_root(path) -> str | None:
    if not path:
        return None
    normalized = _normalize_backend_path(str(path))
    if not posixpath.isabs(normalized):
        return None
    return normalized


def _select_configured_cwd(
    env_type: str, *, config_value: str | None, metadata_value: str | None
) -> str | None:
    if env_type == "local":
        return config_value or metadata_value
    return _backend_configured_root(config_value) or _backend_configured_root(
        metadata_value
    )


def _backend_configured_root(path) -> str | None:
    root = _backend_root(path)
    if root == _host_home_root():
        return None
    return root


def _host_home_root() -> str:
    return _normalize_backend_path(str(Path.home()))


def _normalize_backend_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


def _default_backend_cwd(env_type: str) -> str:
    if env_type == "ssh":
        return "~"
    return "/root"


def _dedupe(paths) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path and path not in seen:
            deduped.append(path)
            seen.add(path)
    return deduped
