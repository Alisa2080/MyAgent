from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional


MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # Keep finished processes for 30 minutes
MAX_PROCESSES = 64              # Max concurrent tracked processes (LRU pruning)

# Watch pattern rate limiting — PER SESSION.
# Hard rule: at most ONE watch-match notification every WATCH_MIN_INTERVAL_SECONDS.
# Any match arriving inside that cooldown window is dropped and counted as a strike.
# After WATCH_STRIKE_LIMIT consecutive strike windows, watch_patterns for that
# session is permanently disabled and the session falls back to notify_on_complete
# semantics (one notification when the process actually exits).
WATCH_MIN_INTERVAL_SECONDS = 15
WATCH_STRIKE_LIMIT = 3

# Global circuit breaker — across all sessions. Secondary safety net so concurrent
# siblings can't collectively flood the user even when each is under its own cap.
WATCH_GLOBAL_MAX_PER_WINDOW = 15
WATCH_GLOBAL_WINDOW_SECONDS = 10
WATCH_GLOBAL_COOLDOWN_SECONDS = 30


def format_uptime_short(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    mins, secs = divmod(s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


@dataclass
class ProcessSession:
    """A tracked background process with output buffering."""
    id: str
    command: str
    task_id: str = ""
    session_key: str = ""
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    env_ref: Any = None
    cwd: Optional[str] = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: Optional[int] = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False
    pid_scope: str = "host"
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_user_id: str = ""
    watcher_user_name: str = ""
    watcher_thread_id: str = ""
    watcher_interval: int = 0
    notify_on_complete: bool = False
    watch_patterns: List[str] = field(default_factory=list)
    network_release: Any = field(default=None, repr=False)
    network_release_error: str = ""
    _watch_hits: int = field(default=0, repr=False)
    _watch_suppressed: int = field(default=0, repr=False)
    _watch_disabled: bool = field(default=False, repr=False)
    _watch_last_emit_at: float = field(default=0.0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)
