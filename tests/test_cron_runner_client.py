"""Tests for cron.runner_client."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class TestRunnerMode:
    def test_default_is_inprocess(self, monkeypatch):
        monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)
        from cron.runner_client import runner_mode

        assert runner_mode() == "inprocess"

    def test_explicit_inprocess(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "inprocess")
        from cron.runner_client import runner_mode

        assert runner_mode() == "inprocess"

    def test_subprocess_mode(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
        from cron.runner_client import runner_mode

        assert runner_mode() == "subprocess"

    def test_process_alias_normalized_to_subprocess(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "process")
        from cron.runner_client import runner_mode

        assert runner_mode() == "subprocess"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "SUBPROCESS")
        from cron.runner_client import runner_mode

        assert runner_mode() == "subprocess"

    def test_empty_env_normalized_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "   ")
        from cron.runner_client import runner_mode

        assert runner_mode() == "inprocess"

    def test_unsupported_mode_accepted_by_runner_mode(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "unknown-mode")
        from cron.runner_client import runner_mode

        assert runner_mode() == "unknown-mode"


class TestRunnerModeValidation:
    def test_inprocess_valid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "inprocess")
        from cron.runner_client import runner_mode_is_valid

        assert runner_mode_is_valid() is True

    def test_subprocess_valid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
        from cron.runner_client import runner_mode_is_valid

        assert runner_mode_is_valid() is True

    def test_unknown_mode_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "unknown-mode")
        from cron.runner_client import runner_mode_is_valid

        assert runner_mode_is_valid() is False


class TestRunnerModeDiagnostic:
    def test_valid_mode(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
        from cron.runner_client import runner_mode_diagnostic

        mode, is_valid = runner_mode_diagnostic()
        assert mode == "subprocess"
        assert is_valid is True

    def test_unknown_mode(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "garbage")
        from cron.runner_client import runner_mode_diagnostic

        mode, is_valid = runner_mode_diagnostic()
        assert mode == "garbage"
        assert is_valid is False


class TestRunJob:
    def test_unsupported_mode_returns_failure_result(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "unknown-mode")
        from cron.runner_client import run_job

        result = run_job({"id": "test-job", "name": "test"})

        assert result.success is False
        assert "unknown-mode" in result.error
        assert "inprocess" in result.error or "subprocess" in result.error

    def test_inprocess_delegates_to_runner(self, monkeypatch):
        from cron.contracts import JobRunResult
        from cron.runner_client import run_job

        fake_result = JobRunResult(success=True, output_doc="doc", final_response="resp")

        with patch("cron.runner.run_job", return_value=fake_result):
            result = run_job({"id": "test"})
            assert result.success is True

    def test_subprocess_mode_imports_runner_subprocess(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        from cron.runner_client import run_job

        with patch("cron.runner_subprocess.run_job_subprocess") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(success=False, error="mocked")
            result = run_job({"id": "test-job", "name": "test"})
            assert result.success is False
            mock_subprocess.assert_called_once()
