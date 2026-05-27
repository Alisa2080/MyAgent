import logging
import tempfile
from pathlib import Path

from agent_cli.logging_config import setup_cli_logging, get_logger


def test_setup_cli_logging_returns_logger():
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "test.log"
        logger = setup_cli_logging(log_file=log_file)
        
        assert logger.name == "agent_cli"
        assert len(logger.handlers) >= 2


def test_setup_cli_logging_creates_file():
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "test.log"
        setup_cli_logging(log_file=log_file)
        
        assert log_file.exists()


def test_setup_cli_logging_defaults_to_logs_directory(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("AGENT_CLI_HOME", tmp)
        logger = setup_cli_logging()

        assert (Path(tmp) / "logs" / "agent.log").exists()
        assert (Path(tmp) / "logs" / "errors.log").exists()
        assert len(logger.handlers) >= 3


def test_setup_cli_logging_writes_to_file():
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "test.log"
        logger = setup_cli_logging(log_file=log_file)
        
        logger.info("Test message")
        
        content = log_file.read_text()
        assert "Test message" in content


def test_get_logger_returns_named_logger():
    logger = get_logger("agent_cli.test")
    assert logger.name == "agent_cli.test"


def test_logger_level_can_be_set():
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "test.log"
        logger = setup_cli_logging(level=logging.DEBUG, log_file=log_file)
        
        assert logger.level == logging.DEBUG
