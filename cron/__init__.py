"""Project-native cron scheduling support."""

import sys

from cron.jobs import (
    JOBS_FILE,
    create_job,
    get_job,
    list_jobs,
    pause_job,
    remove_job,
    resume_job,
    trigger_job,
    update_job,
)
from cron.notifications import drain_cron_notifications_for_thread_id


def __getattr__(name: str):
    if name == "tick":
        from cron.scheduler import tick

        globals()[name] = tick
        return tick
    module = sys.modules.get(f"{__name__}.{name}")
    if module is not None:
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "JOBS_FILE",
    "create_job",
    "drain_cron_notifications_for_thread_id",
    "get_job",
    "list_jobs",
    "pause_job",
    "remove_job",
    "resume_job",
    "tick",
    "trigger_job",
    "update_job",
]
