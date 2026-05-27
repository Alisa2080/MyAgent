from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from agent_cli.paths import ensure_cli_home


def _add_file_handler(
    logger: logging.Logger,
    log_file: Path,
    level: int,
    formatter: logging.Formatter,
) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
    except (PermissionError, OSError):
        return
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def setup_cli_logging(
    level: int = logging.INFO,
    log_file: Path | None = None,
) -> logging.Logger:
    """Setup CLI logging with console and optional file output."""
    logger = logging.getLogger("agent_cli")
    logger.setLevel(level)
    
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    if log_file is None:
        try:
            logs_dir = ensure_cli_home() / "logs"
        except (PermissionError, OSError):
            return logger
        _add_file_handler(logger, logs_dir / "agent.log", logging.DEBUG, formatter)
        _add_file_handler(logger, logs_dir / "errors.log", logging.WARNING, formatter)
    else:
        _add_file_handler(logger, Path(log_file), logging.DEBUG, formatter)
    
    return logger


def get_logger(name: str = "agent_cli") -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)
