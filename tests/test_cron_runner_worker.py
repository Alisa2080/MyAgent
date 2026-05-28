"""Tests for cron.runner_worker entrypoint.

The worker runs in a subprocess so it can be tested end-to-end via the
subprocess boundary. We test argument parsing directly, and protocol-level
behavior is covered in test_cron_runner_subprocess.py (which mocks subprocess.Popen
so no langchain dependency is needed in the subprocess).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


class TestWorkerArgParsing:
    def test_help_flag_exits_successfully(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "cron.runner_worker", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "cron.runner_worker" in result.stdout

    def test_missing_both_args_exits(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "cron.runner_worker"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0

    def test_missing_input_exits(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "cron.runner_worker", "--output", str(tmp_path / "out.json")],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0

    def test_missing_output_exits(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "cron.runner_worker", "--input", str(tmp_path / "in.json")],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0


class TestWorkerMissingInput:
    def test_missing_input_file_writes_failure_result(self, tmp_path):
        """Worker writes a failure result (exit 0) when input file doesn't exist."""
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "nonexistent_input.json"

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
        )

        # Worker writes a failure result and exits 0
        assert result.returncode == 0, result.stderr
        assert output_path.exists()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["success"] is False
        assert "not found" in data["error"]

    def test_result_file_is_owner_only(self, tmp_path):
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "nonexistent_input.json"

        old_umask = os.umask(0o022)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cron.runner_worker",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
            )
        finally:
            os.umask(old_umask)

        assert result.returncode == 0, result.stderr
        assert oct(output_path.stat().st_mode & 0o777) == "0o600"


class TestWorkerInvalidInput:
    def test_invalid_json_writes_failure_result(self, tmp_path):
        """Worker writes a failure result (exit 0) when input JSON is invalid."""
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "input.json"
        input_path.write_text("not json", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
        )

        assert result.returncode == 0, result.stderr
        assert output_path.exists()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["success"] is False
        assert "Invalid input JSON" in data["error"]


class TestWorkerMissingJob:
    def test_missing_job_field_writes_failure_result(self, tmp_path):
        """Worker writes a failure result (exit 0) when payload has no 'job' dict."""
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "input.json"
        input_path.write_text(json.dumps({"version": 1}), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
        )

        assert result.returncode == 0, result.stderr
        assert output_path.exists()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["success"] is False
        assert "missing 'job' dict" in data["error"]


class TestWorkerResultProtocol:
    def test_result_file_schema_is_version_1(self, tmp_path):
        """Worker result file has correct version field.

        Note: This test exercises the worker's failure path since the actual
        run_job execution requires langchain. We verify that when the worker
        does write a result, it uses version=1.
        """
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "input.json"
        # Valid payload but job will fail to execute (no langchain)
        input_path.write_text(
            json.dumps({"version": 1, "job": {"id": "job-1", "name": "x", "prompt": "y"}}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
        )

        # Worker exits 0 (wrote result) — the result may be success or failure
        assert result.returncode == 0
        assert output_path.exists()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert "success" in data
        assert "output_doc" in data
        assert "final_response" in data
        assert "error" in data
