from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cron.state_store import StateStore

logger = logging.getLogger(__name__)


def activity_reporter(
    run_id: str,
    store: StateStore,
    *,
    activity: bool = False,
    last_activity_desc: str | None = None,
    current_tool: str | None = None,
) -> None:
    """Report activity for a cron run.

    Args:
        run_id: The run ID.
        store: The state store.
        activity: If True, records meaningful agent activity (tool call, response).
        last_activity_desc: Human-readable description of the activity.
        current_tool: Name of the tool being executed.
    """
    if os.getenv("AGENT_CRON_DISABLE_ACTIVITY_REPORTING"):
        return
    try:
        store.update_run_activity(
            run_id,
            heartbeat=True,
            activity=activity,
            last_activity_desc=last_activity_desc,
            current_tool=current_tool,
        )
    except Exception:
        logger.debug("Failed to report activity for run %s", run_id, exc_info=True)
