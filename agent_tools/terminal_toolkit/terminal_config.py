from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from agent_core.permissions.profiles import (
    profile_enforces_docker_network,
    resolve_runtime_profile,
    resolve_terminal_env,
)


logger = logging.getLogger(__name__)


def _safe_parse_import_env(name: str, default: Any, converter, type_label: str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return converter(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s: %r (expected %s). Falling back to %r.",
            name,
            raw,
            type_label,
            default,
        )
        return default


def _resolve_container_task_id(task_id: Optional[str]) -> str:
    return task_id or "default"


def _parse_env_var(name: str, default: str, converter=int, type_label: str = "integer"):
    raw = os.getenv(name, default)
    try:
        return converter(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected {type_label})."
        ) from exc


def _get_env_config() -> Dict[str, Any]:
    default_image = "nikolaik/python-nodejs:python3.11-nodejs20"
    profile = resolve_runtime_profile()
    env_type = resolve_terminal_env(profile)
    mount_docker_cwd = os.getenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").lower() in ("true", "1", "yes")
    explicit_network = os.getenv("TERMINAL_CONTAINER_NETWORK")
    if profile_enforces_docker_network(profile) and env_type != "local":
        container_network = False
    elif explicit_network is None:
        container_network = True
    else:
        container_network = explicit_network.lower() in ("true", "1", "yes")

    if env_type == "local":
        default_cwd = os.getcwd()
    elif env_type == "ssh":
        default_cwd = "~"
    else:
        default_cwd = "/root"

    cwd = os.getenv("TERMINAL_CWD", default_cwd)
    if cwd:
        cwd = os.path.expanduser(cwd)
    host_cwd = None
    host_prefixes = ("/Users/", "/home/", "C:\\", "C:/")
    if env_type == "docker" and mount_docker_cwd:
        docker_cwd_source = os.getenv("TERMINAL_CWD") or os.getcwd()
        candidate = os.path.abspath(os.path.expanduser(docker_cwd_source))
        if any(candidate.startswith(p) for p in host_prefixes) or (
            os.path.isabs(candidate) and os.path.isdir(candidate) and not candidate.startswith(("/workspace", "/root"))
        ):
            host_cwd = candidate
            cwd = "/workspace"
    elif env_type in ("docker", "singularity") and cwd:
        is_host_path = any(cwd.startswith(p) for p in host_prefixes)
        is_relative = not os.path.isabs(cwd)
        if (is_host_path or is_relative) and cwd != default_cwd:
            logger.info(
                "Ignoring TERMINAL_CWD=%r for %s backend (host/relative path won't work in sandbox). Using %r instead.",
                cwd,
                env_type,
                default_cwd,
            )
            cwd = default_cwd

    return {
        "runtime_profile": profile,
        "env_type": env_type,
        "docker_image": os.getenv("TERMINAL_DOCKER_IMAGE", default_image),
        "singularity_image": os.getenv("TERMINAL_SINGULARITY_IMAGE", f"docker://{default_image}"),
        "cwd": cwd,
        "host_cwd": host_cwd,
        "docker_mount_cwd_to_workspace": mount_docker_cwd,
        "timeout": _parse_env_var("TERMINAL_TIMEOUT", "180"),
        "lifetime_seconds": _parse_env_var("TERMINAL_LIFETIME_SECONDS", "300"),
        "ssh_host": os.getenv("TERMINAL_SSH_HOST", ""),
        "ssh_user": os.getenv("TERMINAL_SSH_USER", ""),
        "ssh_port": _parse_env_var("TERMINAL_SSH_PORT", "22"),
        "ssh_key": os.getenv("TERMINAL_SSH_KEY", ""),
        "local_persistent": os.getenv("TERMINAL_LOCAL_PERSISTENT", "false").lower() in ("true", "1", "yes"),
        "container_cpu": _parse_env_var("TERMINAL_CONTAINER_CPU", "1", float, "number"),
        "container_memory": _parse_env_var("TERMINAL_CONTAINER_MEMORY", "5120"),
        "container_disk": _parse_env_var("TERMINAL_CONTAINER_DISK", "51200"),
        "container_persistent": os.getenv("TERMINAL_CONTAINER_PERSISTENT", "true").lower() in ("true", "1", "yes"),
        "container_network": container_network,
        "docker_volumes": _parse_env_var("TERMINAL_DOCKER_VOLUMES", "[]", json.loads, "valid JSON"),
        "docker_forward_env": _parse_env_var("TERMINAL_DOCKER_FORWARD_ENV", "[]", json.loads, "valid JSON"),
        "docker_run_as_host_user": os.getenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "false").lower() in ("true", "1", "yes"),
    }


def _image_for_env_type(config: dict, env_type: str) -> str:
    if env_type == "docker":
        return config["docker_image"]
    if env_type == "singularity":
        return config["singularity_image"]
    return ""


def _ssh_config_from_terminal_config(config: dict) -> dict | None:
    if config["env_type"] != "ssh":
        return None
    return {
        "host": config.get("ssh_host", ""),
        "user": config.get("ssh_user", ""),
        "port": config.get("ssh_port", 22),
        "key": config.get("ssh_key", ""),
    }


def _container_config_from_terminal_config(config: dict) -> dict | None:
    if config["env_type"] not in ("docker", "singularity"):
        return None
    return {
        "container_cpu": config.get("container_cpu", 1),
        "container_memory": config.get("container_memory", 5120),
        "container_disk": config.get("container_disk", 51200),
        "container_persistent": config.get("container_persistent", True),
        "container_network": config.get("container_network", True),
        "docker_volumes": config.get("docker_volumes", []),
        "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
        "docker_forward_env": config.get("docker_forward_env", []),
        "docker_env": config.get("docker_env", {}),
        "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
    }
