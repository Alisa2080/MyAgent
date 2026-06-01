"""Parent-side subprocess client for cron job execution.

This module runs each cron job as a child Python process, isolating heavy
dependencies and crash risk from the scheduler process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

from cron.contracts import JobRunResult
from cron.paths import get_runner_tmp_dir, secure_dir, secure_file

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
_SUBPROCESS_TIMEOUT_DEFAULT = 900  # seconds
_TERMINATE_GRACE_DEFAULT = 5  # seconds
_SUBPROCESS_HEARTBEAT_SECONDS = 5.0
_STDERR_MAX_BYTES = 10_000
_STDOUT_MAX_BYTES = 10_000
_COMBINED_SOFT_CAP = _STDERR_MAX_BYTES + _STDOUT_MAX_BYTES
_SMOKE_TIMEOUT_DEFAULT = 10
_TMP_STALE_AFTER_DEFAULT = 24 * 60 * 60
_RUN_DIR_RE = re.compile(r"^[0-9a-f]{16}$")
_SMOKE_DIR_PREFIX = "smoke-"


def _parse_subprocess_timeout() -> int:
    raw = os.getenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "").strip()
    if not raw:
        return _SUBPROCESS_TIMEOUT_DEFAULT
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning(
        "Invalid AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT=%r; using default %d",
        raw,
        _SUBPROCESS_TIMEOUT_DEFAULT,
    )
    return _SUBPROCESS_TIMEOUT_DEFAULT


def _parse_terminate_grace() -> float:
    raw = os.getenv("AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS", "").strip()
    if not raw:
        return float(_TERMINATE_GRACE_DEFAULT)
    try:
        value = float(raw)
        if value >= 0:
            return value
    except ValueError:
        pass
    logger.warning(
        "Invalid AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS=%r; using default %d",
        raw,
        _TERMINATE_GRACE_DEFAULT,
    )
    return float(_TERMINATE_GRACE_DEFAULT)


def subprocess_timeout_diagnostic() -> tuple[int | None, bool]:
    """Return (timeout_seconds, is_valid) for status/doctor output.

    Returns (None, False) when the env var is set to an invalid value.
    """
    raw = os.getenv("AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT", "").strip()
    if not raw:
        return _SUBPROCESS_TIMEOUT_DEFAULT, True
    try:
        value = int(raw)
        if value > 0:
            return value, True
    except ValueError:
        pass
    return None, False


# ----------------------------------------------------------------------
# Temp directory management
# ----------------------------------------------------------------------
def _create_temp_run_dir() -> Path:
    tmp_root = get_runner_tmp_dir()
    tmp_root.parent.mkdir(parents=True, exist_ok=True)
    secure_dir(tmp_root.parent)
    tmp_root.mkdir(parents=True, exist_ok=True)
    secure_dir(tmp_root)
    run_id = uuid.uuid4().hex[:16]
    run_dir = tmp_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    secure_dir(run_dir)
    return run_dir


def _cleanup_temp_dir(tmp_dir: Path) -> None:
    try:
        shutil.rmtree(tmp_dir)
    except OSError as exc:
        logger.debug("Failed to clean up temp dir %s: %s", tmp_dir, exc)


# ----------------------------------------------------------------------
# Result file protocol
# ----------------------------------------------------------------------
_RESULT_VERSION = 1
_REQUIRED_FIELDS = frozenset(
    {"version", "success", "output_doc", "final_response", "error"}
)


class _ResultFileError(Exception):
    """Raised when the result file cannot be parsed as valid JobRunResult."""
    pass


def _parse_result_file(result_path: Path) -> JobRunResult:
    """Parse a result file into JobRunResult.

    Raises _ResultFileError on any parsing/validation failure so the caller
    can distinguish from a missing file (which returns None).
    """
    if not result_path.exists():
        raise _ResultFileError(f"Result file not found: {result_path}")
    try:
        raw = result_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _ResultFileError(f"Failed to read result file: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _ResultFileError(f"Result JSON parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise _ResultFileError(f"Result JSON is not a dict: {type(data).__name__}")
    version = data.get("version")
    if version != _RESULT_VERSION:
        raise _ResultFileError(
            f"Unsupported result version {version!r} (expected {_RESULT_VERSION})"
        )
    if not _REQUIRED_FIELDS.issubset(data.keys()):
        missing = _REQUIRED_FIELDS - set(data.keys())
        raise _ResultFileError(f"Result missing required fields: {missing}")
    if not isinstance(data.get("success"), bool):
        raise _ResultFileError(
            f"Result field 'success' must be bool (got {type(data.get('success')).__name__})"
        )
    for field_name in ("output_doc", "final_response", "error"):
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise _ResultFileError(
                f"Result field {field_name!r} must be string or null "
                f"(got {type(value).__name__})"
            )
    exit_reason = data.get("exit_reason")
    if exit_reason is not None and not isinstance(exit_reason, str):
        raise _ResultFileError("Result field 'exit_reason' must be string or null")
    return JobRunResult(
        success=data["success"],
        output_doc=data.get("output_doc"),
        final_response=data.get("final_response"),
        error=data.get("error"),
        exit_reason=exit_reason,
    )


# ----------------------------------------------------------------------
# Diagnostic capture
# ----------------------------------------------------------------------
def _read_bounded(path: Path | None, max_bytes: int) -> str:
    if path is None or not path.exists():
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    size = len(raw)
    if size <= max_bytes:
        try:
            return raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")
    try:
        head = raw[:max_bytes].decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        head = raw[:max_bytes].decode("latin-1", errors="replace")
    return head + f"\n[... truncated {size - max_bytes} bytes ...]"


def _capture_diagnostics(
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> str:
    parts: list[str] = []
    if stderr_path and stderr_path.exists():
        stderr_text = _read_bounded(stderr_path, _STDERR_MAX_BYTES)
        if stderr_text:
            parts.append(f"[stderr]\n{stderr_text.rstrip()}")
    if stdout_path and stdout_path.exists():
        stdout_text = _read_bounded(stdout_path, _STDOUT_MAX_BYTES)
        if stdout_text:
            parts.append(f"[stdout]\n{stdout_text.rstrip()}")
    combined = "\n".join(parts)
    if len(combined) > _COMBINED_SOFT_CAP:
        combined = combined[:_COMBINED_SOFT_CAP] + "\n[... output truncated ...]"
    return combined


class _BoundedPipeCapture:
    def __init__(self, label: str, max_bytes: int) -> None:
        self.label = label
        self.max_bytes = max_bytes
        self._buffer = bytearray()
        self._truncated = 0

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        remaining = self.max_bytes - len(self._buffer)
        if remaining > 0:
            self._buffer.extend(chunk[:remaining])
        overflow = len(chunk) - max(remaining, 0)
        if overflow > 0:
            self._truncated += overflow

    def text(self) -> str:
        if not self._buffer and not self._truncated:
            return ""
        text = bytes(self._buffer).decode("utf-8", errors="replace")
        if self._truncated:
            text += f"\n[... truncated {self._truncated} bytes ...]"
        return f"[{self.label}]\n{text.rstrip()}"


def _read_pipe(pipe: BinaryIO, capture: _BoundedPipeCapture) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            capture.append(chunk)
    except OSError as exc:
        capture.append(f"[failed to read pipe: {exc}]".encode("utf-8"))
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _start_pipe_reader(
    pipe: BinaryIO | None,
    label: str,
    max_bytes: int,
) -> tuple[_BoundedPipeCapture, threading.Thread | None]:
    capture = _BoundedPipeCapture(label, max_bytes)
    if pipe is None:
        return capture, None
    thread = threading.Thread(target=_read_pipe, args=(pipe, capture), daemon=True)
    thread.start()
    return capture, thread


def _join_pipe_readers(*threads: threading.Thread | None) -> None:
    for thread in threads:
        if thread is not None:
            thread.join(timeout=1)


def _capture_pipe_diagnostics(
    stderr_capture: _BoundedPipeCapture,
    stdout_capture: _BoundedPipeCapture,
) -> str:
    parts = [text for text in (stderr_capture.text(), stdout_capture.text()) if text]
    combined = "\n".join(parts)
    if len(combined) > _COMBINED_SOFT_CAP:
        combined = combined[:_COMBINED_SOFT_CAP] + "\n[... output truncated ...]"
    return combined


def _terminate_child(proc: subprocess.Popen[Any], grace_seconds: float) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.error("Cron runner subprocess still alive after SIGKILL; PID %d", proc.pid)


def _job_payload_for_subprocess(job: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in job.items()
        if not key.startswith("_") and key != "timeout_settings" and not callable(value)
    }
    settings = job.get("timeout_settings")
    if isinstance(settings, dict):
        for key in ("idle_timeout_seconds", "max_runtime_seconds"):
            if key not in payload and settings.get(key) not in (None, ""):
                payload[key] = settings.get(key)
    return payload


def _report_parent_activity(
    job: dict[str, Any],
    desc: str | None = None,
    *,
    heartbeat: bool = True,
    activity: bool = False,
) -> None:
    reporter = job.get("_activity_reporter")
    if callable(reporter):
        reporter(
            heartbeat=heartbeat,
            activity=activity,
            last_activity_desc=desc,
            current_tool=None,
        )


@dataclass(frozen=True)
class WorkerSmokeResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class RunnerTmpSummary:
    total: int
    stale: int
    oldest_age_seconds: int | None
    path: Path


@dataclass(frozen=True)
class RunnerTmpCleanupResult:
    removed: int
    failed: int
    remaining: int
    path: Path


def _is_runner_tmp_child(path: Path) -> bool:
    return path.is_dir() and (
        _RUN_DIR_RE.match(path.name) is not None
        or path.name.startswith(_SMOKE_DIR_PREFIX)
    )


def _runner_tmp_children() -> list[Path]:
    root = get_runner_tmp_dir()
    if not root.exists():
        return []
    return [path for path in root.iterdir() if _is_runner_tmp_child(path)]


def inspect_runner_tmp(
    *,
    stale_after_seconds: int = _TMP_STALE_AFTER_DEFAULT,
) -> RunnerTmpSummary:
    root = get_runner_tmp_dir()
    now = time.time()
    children = _runner_tmp_children()
    ages = [max(0, int(now - child.stat().st_mtime)) for child in children]
    stale_count = sum(1 for age in ages if age >= stale_after_seconds)
    return RunnerTmpSummary(
        total=len(children),
        stale=stale_count,
        oldest_age_seconds=max(ages) if ages else None,
        path=root,
    )


def cleanup_runner_tmp(
    *,
    stale_after_seconds: int = _TMP_STALE_AFTER_DEFAULT,
) -> RunnerTmpCleanupResult:
    root = get_runner_tmp_dir()
    now = time.time()
    removed = 0
    failed = 0
    for child in _runner_tmp_children():
        try:
            age = max(0, int(now - child.stat().st_mtime))
            if age < stale_after_seconds:
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            failed += 1
    remaining = len(_runner_tmp_children())
    return RunnerTmpCleanupResult(
        removed=removed,
        failed=failed,
        remaining=remaining,
        path=root,
    )


def worker_protocol_smoke(
    *,
    timeout_seconds: int = _SMOKE_TIMEOUT_DEFAULT,
) -> WorkerSmokeResult:
    tmp_root = get_runner_tmp_dir()
    tmp_root.parent.mkdir(parents=True, exist_ok=True)
    secure_dir(tmp_root.parent)
    tmp_root.mkdir(parents=True, exist_ok=True)
    secure_dir(tmp_root)
    smoke_dir = tmp_root / f"{_SMOKE_DIR_PREFIX}{uuid.uuid4().hex[:16]}"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    secure_dir(smoke_dir)
    input_path = smoke_dir / "input.json"
    output_path = smoke_dir / "result.json"
    try:
        input_path.write_text(
            json.dumps({"version": _RESULT_VERSION, "smoke": True}),
            encoding="utf-8",
        )
        secure_file(input_path)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--smoke",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            return WorkerSmokeResult(
                False,
                f"runner_worker smoke exited with code {proc.returncode}{suffix}",
            )
        parsed = _parse_result_file(output_path)
        if not parsed.success:
            return WorkerSmokeResult(False, parsed.error or "runner_worker smoke failed")
        return WorkerSmokeResult(True)
    except subprocess.TimeoutExpired:
        return WorkerSmokeResult(
            False,
            f"runner_worker smoke timed out after {timeout_seconds}s",
        )
    except Exception as exc:
        return WorkerSmokeResult(False, str(exc))
    finally:
        _cleanup_temp_dir(smoke_dir)


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
def run_job_subprocess(job: dict[str, Any]) -> JobRunResult:
    """Execute a cron job in a child Python process.

    Uses temporary input/result files for structured communication.
    Enforces a parent-side timeout and cleans up the temp directory after capture.
    """
    tmp_dir = _create_temp_run_dir()
    input_path = tmp_dir / "input.json"
    result_path = tmp_dir / "result.json"

    timed_out = False
    exit_code = -1
    proc: subprocess.Popen[Any] | None = None
    stdout_capture = _BoundedPipeCapture("stdout", _STDOUT_MAX_BYTES)
    stderr_capture = _BoundedPipeCapture("stderr", _STDERR_MAX_BYTES)
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None

    # Write input file
    input_payload = {
        "version": 1,
        "job": _job_payload_for_subprocess(job),
    }
    try:
        input_path.write_text(json.dumps(input_payload, indent=2), encoding="utf-8")
        secure_file(input_path)
        result_path.touch(mode=0o600, exist_ok=True)
        secure_file(result_path)
    except OSError as exc:
        _cleanup_temp_dir(tmp_dir)
        return _make_failure(f"Failed to write input file: {exc}", diagnostics="")

    # Configure timeout
    timeout_seconds = _parse_subprocess_timeout()
    grace_seconds = _parse_terminate_grace()

    python_exe = sys.executable
    worker_module = "cron.runner_worker"
    cmd = [
        python_exe,
        "-m",
        worker_module,
        "--input",
        str(input_path),
        "--output",
        str(result_path),
    ]

    logger.debug(
        "Launching cron runner subprocess: %s (timeout=%ds, grace=%.1fs)",
        " ".join(cmd),
        timeout_seconds,
        grace_seconds,
    )

    # Launch subprocess
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        stdout_capture, stdout_thread = _start_pipe_reader(
            getattr(proc, "stdout", None), "stdout", _STDOUT_MAX_BYTES
        )
        stderr_capture, stderr_thread = _start_pipe_reader(
            getattr(proc, "stderr", None), "stderr", _STDERR_MAX_BYTES
        )
        _report_parent_activity(job, "subprocess_running", activity=True)
        try:
            remaining = float(timeout_seconds)
            while True:
                wait_timeout = min(_SUBPROCESS_HEARTBEAT_SECONDS, remaining)
                try:
                    proc.wait(timeout=wait_timeout)
                    exit_code = proc.returncode
                    break
                except subprocess.TimeoutExpired:
                    if wait_timeout >= remaining:
                        raise
                    remaining -= wait_timeout
                    _report_parent_activity(job, heartbeat=True, activity=False)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "Cron runner subprocess timed out after %ds; terminating PID %d",
                timeout_seconds,
                proc.pid,
            )
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=grace_seconds)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Cron runner subprocess did not exit after SIGTERM; sending SIGKILL"
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=grace_seconds)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    logger.error(
                        "Cron runner subprocess still alive after SIGKILL; PID %d",
                        proc.pid,
                    )
                    exit_code = -1
        except BaseException:
            _terminate_child(proc, grace_seconds)
            _join_pipe_readers(stdout_thread, stderr_thread)
            _cleanup_temp_dir(tmp_dir)
            raise
    except OSError as exc:
        _cleanup_temp_dir(tmp_dir)
        return _make_failure(
            f"Failed to start cron runner subprocess: {exc}",
            diagnostics="",
        )

    # Capture diagnostics
    _join_pipe_readers(stdout_thread, stderr_thread)
    diagnostics = _capture_pipe_diagnostics(stderr_capture, stdout_capture)

    # Try to parse result file
    try:
        result = _parse_result_file(result_path)
    except _ResultFileError as exc:
        # Result file is missing or invalid — map to failure based on what happened
        if timed_out:
            msg = f"Cron runner subprocess timed out after {timeout_seconds} seconds."
        elif exit_code != 0:
            msg = f"Cron runner subprocess exited with code {exit_code}."
        else:
            msg = str(exc)
        _cleanup_temp_dir(tmp_dir)
        return _make_failure(
            msg,
            diagnostics=diagnostics,
            exit_reason="max_runtime_exceeded" if timed_out else None,
        )

    if not result.success:
        result = _enrich_failure_result(result, diagnostics)
    _cleanup_temp_dir(tmp_dir)
    return result


def _make_failure(error: str, diagnostics: str, *, exit_reason: str | None = None) -> JobRunResult:
    error_parts = [error]
    if diagnostics:
        error_parts.append("Diagnostics:")
        error_parts.append(diagnostics)
    return JobRunResult(
        success=False,
        output_doc="\n".join(error_parts) or None,
        final_response=None,
        error=error,
        exit_reason=exit_reason,
    )


def _enrich_failure_result(result: JobRunResult, diagnostics: str) -> JobRunResult:
    """Append bounded diagnostics to output_doc when it is minimal."""
    if not diagnostics:
        return result
    existing = result.output_doc or ""
    if len(existing) < 200:
        output_doc = (
            (f"{existing}\n\nDiagnostics:\n{diagnostics}").strip()
            if existing
            else f"Diagnostics:\n{diagnostics}"
        )
        return replace(result, output_doc=output_doc)
    return result
