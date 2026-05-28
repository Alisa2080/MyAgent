"""Tests for cron.runner_subprocess."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ----------------------------------------------------------------------
# Timeout config
# ----------------------------------------------------------------------
class TestSubprocessTimeout:
    def test_default_timeout(self, monkeypatch):
        monkeypatch.delenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", raising=False)
        from cron.runner_subprocess import _parse_subprocess_timeout
        assert _parse_subprocess_timeout() == 900

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "600")
        from cron.runner_subprocess import _parse_subprocess_timeout
        assert _parse_subprocess_timeout() == 600

    def test_float_timeout_is_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "120.5")
        from cron.runner_subprocess import _parse_subprocess_timeout
        assert _parse_subprocess_timeout() == 900

    def test_zero_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "0")
        from cron.runner_subprocess import _parse_subprocess_timeout
        assert _parse_subprocess_timeout() == 900

    def test_non_numeric_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "not-a-number")
        from cron.runner_subprocess import _parse_subprocess_timeout
        assert _parse_subprocess_timeout() == 900


class TestTerminateGrace:
    def test_default_grace(self, monkeypatch):
        monkeypatch.delenv("AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS", raising=False)
        from cron.runner_subprocess import _parse_terminate_grace
        assert _parse_terminate_grace() == 5.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS", "10")
        from cron.runner_subprocess import _parse_terminate_grace
        assert _parse_terminate_grace() == 10.0

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS", "bad")
        from cron.runner_subprocess import _parse_terminate_grace
        assert _parse_terminate_grace() == 5.0


class TestSubprocessTimeoutDiagnostic:
    def test_default_returns_valid(self, monkeypatch):
        monkeypatch.delenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", raising=False)
        from cron.runner_subprocess import subprocess_timeout_diagnostic
        val, ok = subprocess_timeout_diagnostic()
        assert val == 900
        assert ok is True

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "600")
        from cron.runner_subprocess import subprocess_timeout_diagnostic
        val, ok = subprocess_timeout_diagnostic()
        assert val == 600
        assert ok is True

    def test_invalid_value(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "bad")
        from cron.runner_subprocess import subprocess_timeout_diagnostic
        val, ok = subprocess_timeout_diagnostic()
        assert val is None
        assert ok is False

    def test_float_value_is_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "120.5")
        from cron.runner_subprocess import subprocess_timeout_diagnostic
        val, ok = subprocess_timeout_diagnostic()
        assert val is None
        assert ok is False


# ----------------------------------------------------------------------
# Result file protocol
# ----------------------------------------------------------------------
class TestParseResultFile:
    def test_valid_success_result(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file
        (tmp_path / "result.json").write_text(json.dumps({
            "version": 1,
            "success": True,
            "output_doc": "the output",
            "final_response": "the response",
            "error": None,
        }), encoding="utf-8")
        result = _parse_result_file(tmp_path / "result.json")
        assert result is not None
        assert result.success is True
        assert result.output_doc == "the output"
        assert result.final_response == "the response"
        assert result.error is None

    def test_valid_failure_result(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file
        (tmp_path / "result.json").write_text(json.dumps({
            "version": 1,
            "success": False,
            "output_doc": None,
            "final_response": None,
            "error": "boom",
        }), encoding="utf-8")
        result = _parse_result_file(tmp_path / "result.json")
        assert result is not None
        assert result.success is False
        assert result.error == "boom"

    def test_missing_file_raises(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        with pytest.raises(_ResultFileError, match="not found"):
            _parse_result_file(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        (tmp_path / "result.json").write_text("not json", encoding="utf-8")
        with pytest.raises(_ResultFileError, match="parse error"):
            _parse_result_file(tmp_path / "result.json")

    def test_wrong_version_raises(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        (tmp_path / "result.json").write_text(
            json.dumps({"version": 99, "success": True}), encoding="utf-8")
        with pytest.raises(_ResultFileError, match="Unsupported result version"):
            _parse_result_file(tmp_path / "result.json")

    def test_missing_required_fields_raises(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        (tmp_path / "result.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
        with pytest.raises(_ResultFileError, match="missing required fields"):
            _parse_result_file(tmp_path / "result.json")

    def test_success_must_be_boolean(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        (tmp_path / "result.json").write_text(json.dumps({
            "version": 1,
            "success": "false",
            "output_doc": None,
            "final_response": None,
            "error": None,
        }), encoding="utf-8")
        with pytest.raises(_ResultFileError, match="success"):
            _parse_result_file(tmp_path / "result.json")

    def test_nullable_string_fields_are_required(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file, _ResultFileError
        (tmp_path / "result.json").write_text(json.dumps({
            "version": 1,
            "success": False,
            "output_doc": 123,
            "final_response": None,
            "error": "boom",
        }), encoding="utf-8")
        with pytest.raises(_ResultFileError, match="output_doc"):
            _parse_result_file(tmp_path / "result.json")

    def test_extra_fields_ignored(self, tmp_path):
        from cron.runner_subprocess import _parse_result_file
        (tmp_path / "result.json").write_text(json.dumps({
            "version": 1,
            "success": True,
            "extra_field": "ignored",
            "output_doc": "doc",
            "final_response": None,
            "error": None,
        }), encoding="utf-8")
        result = _parse_result_file(tmp_path / "result.json")
        assert result is not None
        assert result.success is True


# ----------------------------------------------------------------------
# Diagnostic capture
# ----------------------------------------------------------------------
class TestDiagnostics:
    def test_capture_empty(self, tmp_path):
        from cron.runner_subprocess import _capture_diagnostics
        stdout = tmp_path / "stdout.log"
        stderr = tmp_path / "stderr.log"
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        diag = _capture_diagnostics(stdout, stderr)
        assert diag == ""

    def test_capture_bounded(self, tmp_path):
        from cron.runner_subprocess import _capture_diagnostics
        stderr = tmp_path / "stderr.log"
        stdout = tmp_path / "stdout.log"
        stderr.write_text("x" * 20_000, encoding="utf-8")
        stdout.write_text("y" * 20_000, encoding="utf-8")
        diag = _capture_diagnostics(stdout, stderr)
        assert "[stderr]" in diag
        assert "[stdout]" in diag
        # Each stream is individually bounded to 10_000; 11_000 x's would be truncated
        assert ("x" * 11_000) not in diag
        assert ("y" * 11_000) not in diag

    def test_capture_missing_files(self):
        from cron.runner_subprocess import _capture_diagnostics
        assert _capture_diagnostics(None, None) == ""


# ----------------------------------------------------------------------
# run_job_subprocess integration
# ----------------------------------------------------------------------
class TestRunJobSubprocess:
    """These tests mock the subprocess module to avoid needing langchain in the subprocess."""

    def test_parent_interrupt_terminates_child_and_reraises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS", "0")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "interrupt_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        class InterruptingProc:
            pid = 99999
            returncode = None

            def __init__(self):
                self.terminated = False
                self.killed = False
                self.wait_calls = 0
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise KeyboardInterrupt()
                self.returncode = -15
                return self.returncode

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = InterruptingProc()

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", lambda *args, **kwargs: proc):
                from cron.runner_subprocess import run_job_subprocess
                with pytest.raises(KeyboardInterrupt):
                    run_job_subprocess({"id": "job-1", "name": "test", "prompt": "x"})

        assert proc.terminated is True
        assert proc.killed is False

    def test_success_result_parsed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        # Stable temp dir — patch _create_temp_run_dir so we know where result goes
        mock_dir = tmp_path / "cron" / "runner-tmp" / "success_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        # Intercept subprocess.Popen to write a valid result after wait()
        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {
                "pid": 99999,
                "returncode": 0,
                "_mock_dir": mock_dir,
            })()

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text(json.dumps({
                    "version": 1,
                    "success": True,
                    "output_doc": "the output doc",
                    "final_response": "the response",
                    "error": None,
                }), encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "test", "prompt": "x"})
                assert result.success is True
                assert result.output_doc == "the output doc"
                assert result.final_response == "the response"

    def test_worker_failure_result_parsed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "fail_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text(json.dumps({
                    "version": 1,
                    "success": False,
                    "output_doc": "partial",
                    "final_response": None,
                    "error": "agent crashed",
                }), encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "test", "prompt": "x"})
                assert result.success is False
                assert result.error == "agent crashed"

    def test_worker_failure_with_diagnostics_returns_failure_result(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "diag_fail_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()
            proc.stdout = io.BytesIO()
            proc.stderr = io.BytesIO(b"worker stderr detail\n")

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text(json.dumps({
                    "version": 1,
                    "success": False,
                    "output_doc": "partial",
                    "final_response": None,
                    "error": "agent crashed",
                }), encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "test", "prompt": "x"})
                assert result.success is False
                assert result.error == "agent crashed"
                assert result.output_doc is not None
                assert "worker stderr detail" in result.output_doc

    def test_diagnostics_are_captured_from_bounded_pipes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "pipe_diag_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        captured_kwargs = {}

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()
            proc.stdout = io.BytesIO(b"x" * 20_000)
            proc.stderr = io.BytesIO(b"y" * 20_000)

            def do_wait(timeout=None):
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})

        assert captured_kwargs["stdout"] == sp_module.PIPE
        assert captured_kwargs["stderr"] == sp_module.PIPE
        assert result.success is False
        assert result.output_doc is not None
        assert "[stdout]" in result.output_doc
        assert "[stderr]" in result.output_doc
        assert ("x" * 11_000) not in result.output_doc
        assert ("y" * 11_000) not in result.output_doc

    def test_runner_temp_paths_are_owner_only(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "secure_run"
        input_modes = []

        import subprocess as sp_module

        def fake_popen(cmd, **kwargs):
            input_path = Path(cmd[cmd.index("--input") + 1])
            input_modes.append(oct(input_path.stat().st_mode & 0o777))
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()
            proc.stdout = io.BytesIO()
            proc.stderr = io.BytesIO()

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text(json.dumps({
                    "version": 1,
                    "success": True,
                    "output_doc": "doc",
                    "final_response": "final",
                    "error": None,
                }), encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        old_umask = os.umask(0o022)
        try:
            with patch("cron.runner_subprocess.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = "secure_run"
                with patch.object(sp_module, "Popen", fake_popen):
                    from cron.runner_subprocess import run_job_subprocess
                    result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})
        finally:
            os.umask(old_umask)

        assert result.success is True
        assert oct((tmp_path / "cron").stat().st_mode & 0o777) == "0o700"
        assert oct((tmp_path / "cron" / "runner-tmp").stat().st_mode & 0o777) == "0o700"
        assert input_modes == ["0o600"]

    def test_real_worker_protocol_with_stubbed_runner(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        sitecustomize = tmp_path / "sitecustomize.py"
        sitecustomize.write_text(
            "\n".join([
                "import sys",
                "import types",
                "module = types.ModuleType('cron.runner')",
                "class StubResult:",
                "    success = True",
                "    output_doc = 'stub doc job-1'",
                "    final_response = 'stub final'",
                "    error = None",
                "def run_job(job):",
                "    return StubResult()",
                "module.run_job = run_job",
                "sys.modules['cron.runner'] = module",
            ]),
            encoding="utf-8",
        )
        old_pythonpath = os.environ.get("PYTHONPATH")
        pythonpath_parts = [str(tmp_path)]
        if old_pythonpath:
            pythonpath_parts.append(old_pythonpath)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(pythonpath_parts))

        from cron.runner_subprocess import run_job_subprocess

        result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})

        assert result.success is True
        assert result.output_doc == "stub doc job-1"
        assert result.final_response == "stub final"

    def test_timeout_returns_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "2")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "timeout_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": None})()

            def do_wait(timeout=None):
                raise sp_module.TimeoutExpired(cmd=cmd, timeout=timeout or 2)

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "slow", "prompt": "x"})
                assert result.success is False
                assert "timed out" in result.error

    def test_nonzero_exit_without_result_returns_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "5")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "nz_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": 42})()
            proc.wait = lambda timeout=None: 42
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})
                assert result.success is False
                assert "exited with code" in result.error

    def test_invalid_result_json_returns_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "5")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "bad_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text("not json", encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})
                assert result.success is False
                assert "JSON" in result.error or "parse" in result.error

    def test_result_version_mismatch_returns_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "5")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "ver_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

            def do_wait(timeout=None):
                (mock_dir / "result.json").write_text(
                    json.dumps({"version": 99, "success": True}), encoding="utf-8")
                return 0

            proc.wait = do_wait
            proc.terminate = lambda: None
            proc.kill = lambda: None
            return proc

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({"id": "job-1", "name": "x", "prompt": "y"})
                assert result.success is False
