from __future__ import annotations

import concurrent.futures
import errno
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix path is covered in CI/dev
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows path is best-effort fallback
    msvcrt = None

from cron.jobs import (
    advance_next_run,
    get_due_jobs,
    mark_job_run,
    now,
    save_job_output,
)
from cron.notifications import queue_cron_notification, should_notify
from cron.paths import ensure_cron_dirs, get_cron_dir
from cron.runner import JobRunResult, run_job

logger = logging.getLogger(__name__)

_fallback_lock = threading.Lock()


class _TickLockBusy(Exception):
    pass


@dataclass
class JobTickResult:
    job_id: str
    success: bool
    output_path: str | None = None
    error: str | None = None


@dataclass
class TickResult:
    due: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[JobTickResult] = field(default_factory=list)


class _TickLock:
    def __init__(self) -> None:
        self.path = get_cron_dir() / ".tick.lock"
        self.handle = None
        self._locked_with_fcntl = False
        self._locked_with_msvcrt = False
        self._locked_with_fallback = False

    def __enter__(self) -> _TickLock:
        ensure_cron_dirs()
        self.handle = self.path.open("a+")
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked_with_fcntl = True
            elif msvcrt is not None:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                self._locked_with_msvcrt = True
            elif _fallback_lock.acquire(blocking=False):
                self._locked_with_fallback = True
            else:
                raise _TickLockBusy("Cron tick lock is already held.")
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise _TickLockBusy("Cron tick lock is already held.") from exc
            raise
        except BaseException:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.handle is not None:
                if self._locked_with_fcntl and fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                elif self._locked_with_msvcrt and msvcrt is not None:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                self.handle.close()
        finally:
            if self._locked_with_fallback:
                _fallback_lock.release()
            self.handle = None
            self._locked_with_fcntl = False
            self._locked_with_msvcrt = False
            self._locked_with_fallback = False


def _max_parallel(default: int) -> int:
    raw = os.getenv("AGENT_CRON_MAX_PARALLEL")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return default


def _deliver_result(
    job: dict[str, Any],
    result: JobRunResult,
    output_path: str,
    run_at: datetime,
) -> str | None:
    delivery_target = job.get("deliver", "local")
    if delivery_target in {None, "", "local"}:
        return None
    if delivery_target != "origin":
        return f"Unsupported delivery target: {delivery_target}"
    if not result.success or not should_notify(result.final_response):
        return None

    origin = job.get("origin") or {}
    thread_id = origin.get("thread_id")
    if not thread_id:
        return None

    queue_cron_notification(
        str(thread_id),
        {
            "type": "cron_result",
            "job_id": job.get("id"),
            "job_name": job.get("name"),
            "status": "ok",
            "final_response": result.final_response,
            "output_path": output_path,
            "error": None,
            "run_at": run_at.isoformat(),
        },
    )
    return None


def _process_job(job: dict[str, Any], run_at: datetime) -> JobTickResult:
    job_id = str(job["id"])
    try:
        advanced = advance_next_run(job_id, run_at)
        result = run_job(advanced)
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        delivery_error = _deliver_result(advanced, result, output_path, run_at)
        mark_job_run(
            job_id,
            success=result.success,
            error=result.error,
            run_at=run_at,
            delivery_error=delivery_error,
        )
        return JobTickResult(
            job_id=job_id,
            success=result.success,
            output_path=output_path,
            error=result.error or delivery_error,
        )
    except Exception as exc:
        logger.exception("Cron job %s failed during tick.", job_id)
        try:
            mark_job_run(job_id, success=False, error=str(exc), run_at=run_at)
        except Exception:
            logger.exception("Failed to mark cron job %s error state.", job_id)
        return JobTickResult(job_id=job_id, success=False, error=str(exc))


def _run_parallel(jobs: list[dict[str, Any]], run_at: datetime) -> list[JobTickResult]:
    if not jobs:
        return []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_max_parallel(len(jobs))
    ) as pool:
        futures = [pool.submit(_process_job, job, run_at) for job in jobs]
        return [future.result() for future in futures]


def tick(now_dt: datetime | None = None) -> TickResult:
    run_at = now_dt or now()
    lock = _TickLock()
    try:
        lock.__enter__()
    except _TickLockBusy:
        return TickResult(skipped=1)

    try:
        due_jobs = get_due_jobs(now_dt=run_at)
        result = TickResult(due=len(due_jobs))

        workdir_jobs = [job for job in due_jobs if job.get("workdir")]
        parallel_jobs = [job for job in due_jobs if not job.get("workdir")]

        for job in workdir_jobs:
            result.results.append(_process_job(job, run_at))
        result.results.extend(_run_parallel(parallel_jobs, run_at))

        result.ran = len(result.results)
        result.succeeded = sum(1 for item in result.results if item.success)
        result.failed = sum(1 for item in result.results if not item.success)
        return result
    finally:
        lock.__exit__(None, None, None)
