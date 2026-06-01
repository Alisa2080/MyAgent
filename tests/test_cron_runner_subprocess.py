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


def test_worker_protocol_smoke_succeeds_and_cleans_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import worker_protocol_smoke

    result = worker_protocol_smoke(timeout_seconds=10)

    assert result.ok is True
    assert result.error is None
    assert list(get_runner_tmp_dir().iterdir()) == []


def test_worker_protocol_smoke_reports_cleanup_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.runner_subprocess import worker_protocol_smoke

    with patch(
        "cron.runner_subprocess.shutil.rmtree",
        side_effect=OSError("cleanup denied"),
    ):
        result = worker_protocol_smoke(timeout_seconds=10)

    assert result.ok is False
    assert result.error is not None
    assert "cleanup" in result.error.lower()


def test_worker_protocol_smoke_reports_tmp_root_failure(monkeypatch, tmp_path):
    from cron.runner_subprocess import worker_protocol_smoke

    blocked_home = tmp_path / "blocked-home"
    blocked_home.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        "cron.runner_subprocess.get_runner_tmp_dir",
        lambda: blocked_home / "runner-tmp",
    )

    result = worker_protocol_smoke(timeout_seconds=10)

    assert result.ok is False
    assert result.severity == "fail"
    assert result.error is not None
    assert "runner tmp" in result.error.lower()


def test_worker_protocol_smoke_timeout_is_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.runner_subprocess import worker_protocol_smoke

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["runner"], timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)

    result = worker_protocol_smoke(timeout_seconds=1)

    assert result.ok is False
    assert result.severity == "warn"
    assert result.error == "runner_worker smoke timed out after 1s"


def test_runner_tmp_residuals_report_old_directory(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import inspect_runner_tmp

    old_dir = get_runner_tmp_dir() / "abcdef1234567890"
    old_dir.mkdir(parents=True)
    old_time = time.time() - (25 * 60 * 60)
    os.utime(old_dir, (old_time, old_time))

    summary = inspect_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert summary.total == 1
    assert summary.stale == 1
    assert summary.oldest_age_seconds is not None
    assert summary.oldest_age_seconds >= 24 * 60 * 60


def test_inspect_runner_tmp_skips_vanished_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import inspect_runner_tmp

    vanished = get_runner_tmp_dir() / "abcdef1234567890"
    vanished.mkdir(parents=True)

    def children_after_listing():
        vanished.rmdir()
        return [vanished]

    monkeypatch.setattr(
        "cron.runner_subprocess._runner_tmp_children",
        children_after_listing,
    )

    summary = inspect_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert summary.total == 0
    assert summary.stale == 0
    assert summary.oldest_age_seconds is None


def test_inspect_runner_tmp_reports_unusable_root(monkeypatch):
    from cron.runner_subprocess import inspect_runner_tmp

    monkeypatch.setattr(
        "cron.runner_subprocess._runner_tmp_children",
        lambda: (_ for _ in ()).throw(OSError("permission denied")),
    )

    summary = inspect_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert summary.total == 0
    assert summary.stale == 0
    assert summary.oldest_age_seconds is None
    assert summary.error == "permission denied"


def test_cleanup_runner_tmp_removes_only_old_valid_dirs(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import cleanup_runner_tmp

    root = get_runner_tmp_dir()
    old_valid = root / "abcdef1234567890"
    fresh_valid = root / "1234567890abcdef"
    old_smoke = root / "smoke-abcdef1234567890"
    fresh_smoke = root / "smoke-1234567890abcdef"
    invalid = root / "not-a-run-dir"
    for path in (old_valid, fresh_valid, old_smoke, fresh_smoke, invalid):
        path.mkdir(parents=True, exist_ok=True)
    old_time = time.time() - (25 * 60 * 60)
    os.utime(old_valid, (old_time, old_time))
    os.utime(old_smoke, (old_time, old_time))

    result = cleanup_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert result.removed == 2
    assert result.failed == 0
    assert not old_valid.exists()
    assert not old_smoke.exists()
    assert fresh_valid.exists()
    assert fresh_smoke.exists()
    assert invalid.exists()


def test_cleanup_runner_tmp_ignores_invalid_smoke_prefix_dir(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import cleanup_runner_tmp

    root = get_runner_tmp_dir()
    valid_smoke = root / "smoke-abcdef1234567890"
    invalid_smoke = root / "smoke-not-a-run-dir"
    valid_smoke.mkdir(parents=True)
    invalid_smoke.mkdir(parents=True)
    old_time = time.time() - (25 * 60 * 60)
    os.utime(valid_smoke, (old_time, old_time))
    os.utime(invalid_smoke, (old_time, old_time))

    result = cleanup_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert result.removed == 1
    assert result.failed == 0
    assert not valid_smoke.exists()
    assert invalid_smoke.exists()


def test_cleanup_runner_tmp_reports_unusable_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import cleanup_runner_tmp

    root = get_runner_tmp_dir()
    root.parent.mkdir(parents=True)
    root.write_text("not a directory", encoding="utf-8")

    result = cleanup_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert result.removed == 0
    assert result.failed == 1
    assert result.remaining == 0
    assert result.error is not None
    assert "not a directory" in result.error.lower()


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

    def test_parent_reports_activity_while_subprocess_runs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "heartbeat_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        activity_events = []

        def reporter(**kwargs):
            activity_events.append(kwargs)

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            proc = type("MockP", (), {"pid": 99999, "returncode": None})()
            proc.wait_calls = 0

            def do_wait(timeout=None):
                proc.wait_calls += 1
                if proc.wait_calls == 1:
                    raise sp_module.TimeoutExpired(cmd=cmd, timeout=timeout)
                proc.returncode = 0
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

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({
                    "id": "job-1",
                    "name": "test",
                    "prompt": "x",
                    "_activity_reporter": reporter,
                })

        assert result.success is True
        assert activity_events[0]["activity"] is True
        assert activity_events[0]["last_activity_desc"] == "subprocess_running"
        assert any(
            event["heartbeat"] is True and event["activity"] is False
            for event in activity_events[1:]
        )

    def test_subprocess_input_excludes_runtime_only_fields(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "payload_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        captured_payload = {}

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            input_path = Path(cmd[cmd.index("--input") + 1])
            captured_payload.update(json.loads(input_path.read_text(encoding="utf-8")))
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

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

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({
                    "id": "job-1",
                    "name": "test",
                    "prompt": "x",
                    "timeout_settings": {"idle_timeout_seconds": 60},
                    "_activity_reporter": lambda **kwargs: None,
                    "_private": "hidden",
                })

        assert result.success is True
        assert captured_payload["job"]["id"] == "job-1"
        assert "timeout_settings" not in captured_payload["job"]
        assert "_activity_reporter" not in captured_payload["job"]
        assert "_private" not in captured_payload["job"]

    def test_subprocess_input_materializes_timeout_settings_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "timeout_payload_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        captured_payload = {}

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            input_path = Path(cmd[cmd.index("--input") + 1])
            captured_payload.update(json.loads(input_path.read_text(encoding="utf-8")))
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

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

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({
                    "id": "job-1",
                    "name": "test",
                    "prompt": "x",
                    "timeout_settings": {
                        "idle_timeout_seconds": 11,
                        "max_runtime_seconds": 22,
                    },
                })

        assert result.success is True
        assert captured_payload["job"]["idle_timeout_seconds"] == 11
        assert captured_payload["job"]["max_runtime_seconds"] == 22
        assert "timeout_settings" not in captured_payload["job"]

    def test_subprocess_input_schema_timeout_precedes_timeout_settings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "30")

        mock_dir = tmp_path / "cron" / "runner-tmp" / "timeout_precedence_run"
        mock_dir.mkdir(parents=True, exist_ok=True)

        import subprocess as sp_module

        captured_payload = {}

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            input_path = Path(cmd[cmd.index("--input") + 1])
            captured_payload.update(json.loads(input_path.read_text(encoding="utf-8")))
            proc = type("MockP", (), {"pid": 99999, "returncode": 0})()

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

        with patch("cron.runner_subprocess._create_temp_run_dir", return_value=mock_dir):
            with patch.object(sp_module, "Popen", fake_popen):
                from cron.runner_subprocess import run_job_subprocess
                result = run_job_subprocess({
                    "id": "job-1",
                    "name": "test",
                    "prompt": "x",
                    "idle_timeout_seconds": 33,
                    "max_runtime_seconds": 44,
                    "timeout_settings": {
                        "idle_timeout_seconds": 11,
                        "max_runtime_seconds": 22,
                    },
                })

        assert result.success is True
        assert captured_payload["job"]["idle_timeout_seconds"] == 33
        assert captured_payload["job"]["max_runtime_seconds"] == 44
        assert "timeout_settings" not in captured_payload["job"]

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
                assert result.exit_reason == "max_runtime_exceeded"

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
