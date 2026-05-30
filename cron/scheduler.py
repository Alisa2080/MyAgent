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
    compute_next_run,
    get_due_jobs,
    mark_job_run,
    now,
    save_job_output,
)
from cron.contracts import JobRunner
from cron.delivery_store import DeliveryStore
from cron.paths import ensure_cron_dirs, get_cron_dir
from cron.state_store import StateStore

logger = logging.getLogger(__name__)

_fallback_lock = threading.Lock()


def _store() -> StateStore:
    return StateStore()


def _default_job_runner() -> JobRunner:
    from cron.runner_client import run_job

    return run_job


def _run_default_job(job: dict[str, Any]):
    return _default_job_runner()(job)


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


def _delivery_error_from_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    if event.get("status") in {"failed", "dead"}:
        return str(event.get("last_error") or "delivery target is not deliverable")
    return None


def _next_run_after_completion(job: dict[str, Any], run_at: datetime) -> tuple[str | None, bool]:
    repeat = dict(job.get("repeat") or {"times": None, "completed": 0})
    repeat["completed"] = int(repeat.get("completed") or 0) + 1
    repeat_times = repeat.get("times")
    completed = repeat_times is not None and repeat["completed"] >= int(repeat_times)
    if completed:
        return None, True
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind == "once":
        return None, True
    if kind in {"interval", "cron"}:
        return compute_next_run(schedule, base=run_at), False
    return None, False


def _process_claimed(
    claimed: dict[str, Any],
    run_at: datetime,
    job_runner: JobRunner,
) -> JobTickResult:
    store = _store()
    job = dict(claimed["job"])
    run = dict(claimed["run"])
    job_id = str(job["id"])
    run_id = run["id"]
    job["run_id"] = run_id
    now_text = run_at.isoformat()
    try:
        if store.mark_run_started(run_id) is None:
            return JobTickResult(job_id=job_id, success=False, error="run lease expired before start")
        result = job_runner(job)
        if not store.run_owns_lease(run_id):
            store.complete_run(
                run["id"],
                success=False,
                output_path=None,
                final_response=None,
                error="run lease lost before delivery",
                next_run_at=job.get("next_run_at"),
                completed=False,
            )
            return JobTickResult(job_id=job_id, success=False, error="run lease lost before delivery")
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        from cron.delivery import enqueue_result, process_due

        delivery_store = DeliveryStore(store.path)
        delivery_events = enqueue_result(job, result, output_path, run_at, store=delivery_store)
        if not store.run_owns_lease(run["id"]):
            events = delivery_events if isinstance(delivery_events, list) else ([delivery_events] if delivery_events else [])
            delivery_store.delete_events([str(event["id"]) for event in events])
            store.complete_run(
                run["id"],
                success=False,
                output_path=output_path,
                final_response=result.final_response,
                error="run lease lost before dispatch",
                next_run_at=job.get("next_run_at"),
                completed=False,
            )
            return JobTickResult(job_id=job_id, success=False, output_path=output_path, error="run lease lost before dispatch")
        process_due(limit=20, store=delivery_store)
        next_run_at, completed = _next_run_after_completion(job, run_at)
        delivery_error = None
        if delivery_events:
            events = delivery_events if isinstance(delivery_events, list) else [delivery_events]
            for evt in events:
                try:
                    evt_data = delivery_store.get(evt["id"])
                    err = _delivery_error_from_event(evt_data)
                    if err:
                        delivery_error = err
                except KeyError:
                    pass
            store.update_run_delivery_status(run["id"])
        store.complete_run(
            run["id"],
            success=result.success,
            output_path=output_path,
            final_response=result.final_response,
            error=result.error,
            next_run_at=next_run_at,
            completed=completed,
            delivery_error=delivery_error,
        )
        return JobTickResult(job_id=job_id, success=result.success, output_path=output_path, error=result.error or delivery_error)
    except Exception as exc:
        logger.exception("Cron job %s failed during tick.", job_id)
        next_run_at, completed = _next_run_after_completion(job, run_at)
        store.complete_run(
            run["id"],
            success=False,
            output_path=None,
            final_response=None,
            error=str(exc),
            next_run_at=next_run_at,
            completed=completed,
        )
        return JobTickResult(job_id=job_id, success=False, error=str(exc))


def _process_job(
    job: dict[str, Any],
    run_at: datetime,
    job_runner: JobRunner,
) -> JobTickResult:
    job_id = str(job["id"])
    try:
        advanced = advance_next_run(job_id, run_at)
        result = job_runner(advanced)
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        from cron.delivery import enqueue_result, process_due

        delivery_event = enqueue_result(advanced, result, output_path, run_at)
        process_due(limit=20)
        if delivery_event:
            try:
                delivery_event = DeliveryStore().get(delivery_event["id"])
            except KeyError:
                pass
        delivery_error = _delivery_error_from_event(delivery_event)
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


def _run_parallel(
    jobs: list[dict[str, Any]],
    run_at: datetime,
    job_runner: JobRunner,
) -> list[JobTickResult]:
    if not jobs:
        return []

    def process(job: dict[str, Any]) -> JobTickResult:
        return _process_job(job, run_at, job_runner)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_max_parallel(len(jobs))
    ) as pool:
        futures = [pool.submit(process, job) for job in jobs]
        return [future.result() for future in futures]


def _run_parallel_claimed(
    claimed_items: list[dict[str, Any]],
    run_at: datetime,
    job_runner: JobRunner,
) -> list[JobTickResult]:
    if not claimed_items:
        return []

    def process(claimed: dict[str, Any]) -> JobTickResult:
        return _process_claimed(claimed, run_at, job_runner)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_max_parallel(len(claimed_items))
    ) as pool:
        futures = [pool.submit(process, item) for item in claimed_items]
        return [future.result() for future in futures]


def tick(
    now_dt: datetime | None = None,
    *,
    job_runner: JobRunner | None = None,
) -> TickResult:
    run_at = now_dt or now()
    lock = _TickLock()
    try:
        lock.__enter__()
    except _TickLockBusy:
        return TickResult(skipped=1)

    try:
        store = _store()
        store.recover_expired_leases(now_text=run_at.isoformat())
        claimed = store.claim_due_jobs(now_text=run_at.isoformat(), limit=100)
        result = TickResult(due=len(claimed))

        if not claimed:
            return result

        resolved_runner: JobRunner = job_runner if job_runner is not None else _run_default_job
        workdir_claimed = [item for item in claimed if item["job"].get("workdir")]
        parallel_claimed = [item for item in claimed if not item["job"].get("workdir")]

        for item in workdir_claimed:
            result.results.append(_process_claimed(item, run_at, resolved_runner))
        result.results.extend(_run_parallel_claimed(parallel_claimed, run_at, resolved_runner))

        result.ran = len(result.results)
        result.succeeded = sum(1 for item in result.results if item.success)
        result.failed = sum(1 for item in result.results if not item.success)
        return result
    finally:
        lock.__exit__(None, None, None)
