from __future__ import annotations

import logging
import threading

import cron.scheduler

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _ticker_loop(interval_seconds: float) -> None:
    while not _stop_event.is_set():
        try:
            cron.scheduler.tick()
        except Exception:
            logger.exception("Cron scheduler tick failed.")
        _stop_event.wait(interval_seconds)


def start_cron_scheduler(interval_seconds: float = 60) -> bool:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False

        _stop_event.clear()
        interval = max(1.0, float(interval_seconds))
        _thread = threading.Thread(
            target=_ticker_loop,
            args=(interval,),
            name="cron-ticker",
            daemon=True,
        )
        _thread.start()
        return True


def stop_cron_scheduler(timeout: float | None = None) -> bool:
    global _thread
    with _lock:
        thread = _thread
        if thread is None:
            _stop_event.set()
            return False
        _stop_event.set()

    thread.join(timeout=timeout)

    with _lock:
        stopped = not thread.is_alive()
        if stopped:
            _thread = None
        return stopped


def is_cron_scheduler_running() -> bool:
    thread = _thread
    return bool(thread is not None and thread.is_alive())
