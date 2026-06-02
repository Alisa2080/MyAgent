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
class DeliveryTickSummary:
    recovered_stale: int = 0
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    error: str | None = None


@dataclass
class JobTickResult:
    job_id: str
    success: bool
    output_path: str | None = None
    error: str | None = None
    delivery: DeliveryTickSummary = field(default_factory=DeliveryTickSummary)


@dataclass
class TickResult:
    due: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    delivery: DeliveryTickSummary = field(default_factory=DeliveryTickSummary)
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


def _exit_reason_for_stale_reason(stale_reason: str | None) -> str:
    if stale_reason == "idle_timeout_exceeded":
        return "idle_timeout"
    if stale_reason == "heartbeat_stale":
        return "idle_timeout"
    return str(stale_reason or "abandoned")


def _complete_stale_run(
    store: StateStore,
    stale: dict[str, Any],
    run_at: datetime,
) -> None:
    job = store.get_job(str(stale["job_id"]))
    next_run_at, completed = _next_run_after_completion(job or stale, run_at)
    stale_reason = str(stale.get("stale_reason") or "stale")
    store.complete_run(
        str(stale["run_id"]),
        success=False,
        output_path=None,
        final_response=None,
        error=f"Job abandoned: {stale_reason}",
        next_run_at=next_run_at,
        completed=completed,
        exit_reason=_exit_reason_for_stale_reason(stale_reason),
        advance_schedule=not bool(stale.get("preserve_schedule")),
    )


def _process_claimed(
    claimed: dict[str, Any],
    run_at: datetime,
    job_runner: JobRunner,
) -> JobTickResult:
    store = _store()
    job = dict(claimed["job"])
    run = dict(claimed["run"])
    preserve_schedule = bool(run.get("preserve_schedule"))
    job_id = str(job["id"])
    run_id = run["id"]
    job["run_id"] = run_id
    try:
        from cron.activity_reporter import activity_reporter

        if store.mark_run_started(run_id) is None:
            return JobTickResult(job_id=job_id, success=False, error="run lease expired before start")
        try:
            from cron.activity_reporter import activity_reporter
            activity_reporter(run_id, store, activity=True, last_activity_desc="started")
        except Exception:
            logger.debug("Failed to report started activity for run %s", run_id)

        try:
            from cron.activity_reporter import activity_reporter

            job["_activity_reporter"] = lambda **kwargs: activity_reporter(
                run_id,
                store,
                **kwargs,
            )
        except Exception:
            logger.debug("Failed to attach activity reporter for run %s", run_id)

        # Check if this run has become stale before starting (race condition protection)
        stale_runs = store.list_stale_running_runs(limit=10)
        for stale in stale_runs:
            if stale["run_id"] == run_id and stale.get("stale_reason") in ("idle_timeout_exceeded", "heartbeat_stale"):
                _complete_stale_run(store, stale, run_at)
                return JobTickResult(job_id=job_id, success=False, error=f"Job abandoned: {stale['stale_reason']}")

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
                advance_schedule=not preserve_schedule,
            )
            return JobTickResult(job_id=job_id, success=False, error="run lease lost before delivery")
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        from cron.delivery import enqueue_result, process_due

        delivery_store = DeliveryStore(store.path)
        delivery_job = {key: value for key, value in job.items() if not key.startswith("_")}
        delivery_events = enqueue_result(delivery_job, result, output_path, run_at, store=delivery_store)
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
                advance_schedule=not preserve_schedule,
            )
            return JobTickResult(job_id=job_id, success=False, output_path=output_path, error="run lease lost before dispatch")
        dispatched = process_due(limit=20, store=delivery_store)
        delivery_summary = _delivery_summary_from_dispatch(dispatched)
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
            exit_reason=result.exit_reason,
            advance_schedule=not preserve_schedule,
        )
        try:
            from cron.activity_reporter import activity_reporter
            activity_reporter(run["id"], store, activity=True, last_activity_desc="completed")
        except Exception:
            logger.debug("Failed to report completed activity for run %s", run["id"])
        return JobTickResult(
            job_id=job_id,
            success=result.success,
            output_path=output_path,
            error=result.error or delivery_error,
            delivery=delivery_summary,
        )
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
            exit_reason=getattr(exc, "exit_reason", None),
            advance_schedule=not preserve_schedule,
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
        dispatched = process_due(limit=20)
        delivery_summary = _delivery_summary_from_dispatch(dispatched)
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
            delivery=delivery_summary,
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


def _format_delivery_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _delivery_summary_from_dispatch(dispatched: dict[str, int]) -> DeliveryTickSummary:
    return DeliveryTickSummary(
        claimed=int(dispatched.get("claimed", 0) or 0),
        delivered=int(dispatched.get("delivered", 0) or 0),
        failed=int(dispatched.get("failed", 0) or 0),
        dead=int(dispatched.get("dead", 0) or 0),
    )


def _merge_delivery_summary(
    target: DeliveryTickSummary,
    source: DeliveryTickSummary,
) -> None:
    target.recovered_stale += source.recovered_stale
    target.claimed += source.claimed
    target.delivered += source.delivered
    target.failed += source.failed
    target.dead += source.dead
    if source.error and not target.error:
        target.error = source.error


def _process_delivery_maintenance(store: StateStore, *, limit: int = 20) -> DeliveryTickSummary:
    summary = DeliveryTickSummary()
    try:
        summary.recovered_stale = store.recover_stale_delivery_events()
    except Exception as exc:
        summary.error = _format_delivery_error(exc)
        logger.exception("Cron delivery stale recovery failed during tick.")
        return summary

    try:
        from cron.delivery import process_due

        dispatched = process_due(limit=limit, store=store, recover_stale=False)
    except Exception as exc:
        summary.error = _format_delivery_error(exc)
        logger.exception("Cron delivery dispatch failed during tick.")
        return summary

    dispatched_summary = _delivery_summary_from_dispatch(dispatched)
    dispatched_summary.recovered_stale = summary.recovered_stale
    return dispatched_summary


def tick(
    now_dt: datetime | None = None,
    *,
    job_runner: JobRunner | None = None,
    now_text: str | None = None,
) -> TickResult:
    if now_text:
        from cron.state_store import _parse_time
        parsed = _parse_time(now_text)
        run_at = parsed if parsed is not None else (now_dt or now())
    else:
        run_at = now_dt or now()
    lock = _TickLock()
    try:
        lock.__enter__()
    except _TickLockBusy:
        return TickResult(skipped=1)

    try:
        store = _store()
        store.recover_expired_leases(now_text=run_at.isoformat())

        # Abandon idle-timed-out runs before processing new jobs
        stale_runs = store.list_stale_running_runs(limit=10)
        for stale in stale_runs:
            if stale.get("stale_reason") in ("idle_timeout_exceeded", "heartbeat_stale"):
                _complete_stale_run(store, stale, run_at)

        result = TickResult()
        result.delivery = _process_delivery_maintenance(store, limit=20)

        claimed = store.claim_ready_manual_runs(limit=100)
        claimed = claimed + store.promote_queued_runs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
        claimed = claimed + store.claim_due_jobs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
        result.due = len(claimed)

        if not claimed:
            return result

        resolved_runner: JobRunner = job_runner if job_runner is not None else _run_default_job
        result.results.extend(_run_parallel_claimed(claimed, run_at, resolved_runner))

        result.ran = len(result.results)
        result.succeeded = sum(1 for item in result.results if item.success)
        result.failed = sum(1 for item in result.results if not item.success)
        for item in result.results:
            _merge_delivery_summary(result.delivery, item.delivery)
        return result
    finally:
        lock.__exit__(None, None, None)
