#!/usr/bin/env python3
"""File Tools Module - LLM agent file manipulation tools."""

import errno
import json
import logging
import os
import threading
from contextlib import nullcontext
from pathlib import Path

from agent_tools.file_toolkit.backend_paths import (
    get_backend_path_context,
    resolve_path_for_policy,
)
from agent_tools.file_toolkit.binary_extensions import has_binary_extension
from agent_tools.file_toolkit.file_operations import (
    ShellFileOperations,
    normalize_read_pagination,
    normalize_search_pagination,
)
from agent_tools.file_toolkit import file_state
from agent_tools.file_toolkit.patch_parser import parse_v4a_patch
from agent_tools.file_toolkit.redact import redact_sensitive_text
from agent_tools.terminal_toolkit.terminal_tool import (
    get_active_env,
    get_or_create_active_env,
)

logger = logging.getLogger(__name__)


_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}


def _approved_write_roots_context(file_ops, roots: list[str] | None):
    approved_roots = getattr(file_ops, "approved_write_roots", None)
    if approved_roots is None:
        return nullcontext()
    return approved_roots(roots)


def tool_error(message: str, **extra) -> str:
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Read-size guard: cap the character count returned to the model.
# We're model-agnostic so we can't count tokens; characters are a safe proxy.
# 100K chars ≈ 25–35K tokens across typical tokenisers.  Files larger than
# this in a single read are a context-window hazard — the model should use
# offset+limit to read the relevant section.
#
# Configurable via environment: `AGENT_FILE_READ_MAX_CHARS=200000`
# ---------------------------------------------------------------------------
_DEFAULT_MAX_READ_CHARS = 100_000
_max_read_chars_cached: int | None = None


def _get_max_read_chars() -> int:
    """Return the configured max characters per file read.

    Reads ``AGENT_FILE_READ_MAX_CHARS`` on first call and caches the
    result for the lifetime of the process. Falls back to the built-in
    default if the value is missing or invalid.
    """
    global _max_read_chars_cached
    if _max_read_chars_cached is not None:
        return _max_read_chars_cached
    val = os.getenv("AGENT_FILE_READ_MAX_CHARS")
    if val:
        try:
            parsed = int(val)
            if parsed > 0:
                _max_read_chars_cached = parsed
                return _max_read_chars_cached
        except ValueError:
            pass
    _max_read_chars_cached = _DEFAULT_MAX_READ_CHARS
    return _max_read_chars_cached

# If the total file size exceeds this AND the caller didn't specify a narrow
# range (limit <= 200), we include a hint encouraging targeted reads.
_LARGE_FILE_HINT_BYTES = 512_000  # 512 KB

# ---------------------------------------------------------------------------
# Device path blocklist — reading these hangs the process (infinite output
# or blocking on input).  Checked by path only (no I/O).
# ---------------------------------------------------------------------------
_BLOCKED_DEVICE_PATHS = frozenset({
    # Infinite output — never reach EOF
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    # Blocks waiting for input
    "/dev/stdin", "/dev/tty", "/dev/console",
    # Nonsensical to read
    "/dev/stdout", "/dev/stderr",
    # fd aliases
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _resolve_path(filepath: str, task_id: str = "default") -> Path:
    """Resolve a path relative to TERMINAL_CWD (the worktree base directory)
    instead of the main repository root.
    """
    return _resolve_path_for_task(filepath, task_id)


def _get_live_tracking_cwd(task_id: str = "default") -> str | None:
    """Return the task's live terminal cwd for bookkeeping when available."""
    effective_task_id = task_id or "default"
    active_env = get_active_env(effective_task_id)
    live_cwd = getattr(active_env, "cwd", None)
    return live_cwd or None

def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path:
    """Resolve *filepath* against backend-aware live cwd when possible."""
    return Path(resolve_path_for_policy(filepath, task_id))


def _sample_mtime_for_task(resolved_path: str, task_id: str = "default") -> float | None:
    """Sample mtime from the correct filesystem for this task.

    Local environments use host os.path.getmtime. Docker, Singularity, and SSH
    use the active terminal toolkit backend shell. A None result is an explicit
    degradation: dedup and external-drift checks are skipped, but file_state
    still records reads/writes for sibling-writer coordination.
    """
    effective_task_id = task_id or "default"
    try:
        ctx = get_backend_path_context(effective_task_id)
    except Exception:
        logger.debug("Failed to resolve backend context for mtime sampling", exc_info=True)
        return None

    if ctx is not None and ctx.env_type != "local":
        try:
            file_ops = _get_file_ops(effective_task_id)
            stat_mtime = getattr(file_ops, "stat_mtime", None)
            if stat_mtime is None:
                return None
            return stat_mtime(str(resolved_path))
        except Exception:
            logger.debug(
                "Failed to sample backend mtime for %s in task %s",
                resolved_path,
                effective_task_id,
                exc_info=True,
            )
            return None

    try:
        return os.path.getmtime(str(resolved_path))
    except OSError:
        return None


def _is_blocked_device(filepath: str) -> bool:
    """Return True if the path would hang the process (infinite output or blocking input).

    Uses the *literal* path — no symlink resolution — because the model
    specifies paths directly and realpath follows symlinks all the way
    through (e.g. /dev/stdin → /proc/self/fd/0 → /dev/pts/0), defeating
    the check.
    """
    normalized = os.path.expanduser(filepath)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 and /proc/<pid>/fd/0-2 are Linux aliases for stdio
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    return False


# Paths that file tools should refuse to write to without going through the
# terminal tool's approval system.  These match prefixes after os.path.realpath.
_SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/", "/private/var/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}


def _check_sensitive_path(filepath: str, task_id: str = "default") -> str | None:
    """Return an error message if the path targets a sensitive system location."""
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(os.path.expanduser(filepath))
    _err = (
        f"Refusing to write to sensitive system path: {filepath}\n"
        "Use the terminal tool with sudo if you need to modify system files."
    )
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _err
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _err
    return None


def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False


_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}

# Track files read per task to detect re-read loops and deduplicate reads.
# Per task_id we store:
#   "last_key":     the key of the most recent read/search call (or None)
#   "consecutive":  how many times that exact call has been repeated in a row
#   "read_history": set of (path, offset, limit) tuples for get_read_files_summary
#   "dedup":        dict mapping (resolved_path, offset, limit) → mtime float
#                   Used to skip re-reads of unchanged files.  Reset on
#                   context compression (the original content is summarised
#                   away so the model needs the full content again).
#   "read_timestamps": dict mapping resolved_path → modification-time float
#                      recorded when the file was last read (or written) by
#                      this task.  Used by write_file and patch to detect
#                      external changes between the agent's read and write.
#                      Updated after successful writes so consecutive edits
#                      by the same task don't trigger false warnings.
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}

# Per-task bounds for the containers inside each _read_tracker[task_id].
# A CLI session uses one stable task_id for its lifetime; without these
# caps, a 10k-read session would accumulate ~1.5MB of dict/set state that
# is never referenced again (only the most recent reads matter for dedup,
# loop detection, and external-edit warnings).  Hard caps bound the
# accretion to a few hundred KB regardless of session length.
_READ_HISTORY_CAP = 500       # set; used only by get_read_files_summary
_DEDUP_CAP = 1000             # dict; skip-identical-reread guard
_READ_TIMESTAMPS_CAP = 1000   # dict; external-edit detection for write/patch
_READ_DEDUP_STATUS_MESSAGE = (
    "File unchanged since last read. The content from "
    "the earlier read_file result in this conversation is "
    "still current — refer to that instead of re-reading."
)


def _cap_read_tracker_data(task_data: dict) -> None:
    """Enforce size caps on the per-task read-tracker sub-containers.

    Must be called with ``_read_tracker_lock`` held.  Eviction policy:

      * ``read_history`` (set): pop arbitrary entries on overflow.  This
        is fine because the set only feeds diagnostic summaries; losing
        old entries just trims the summary's tail.
      * ``dedup`` / ``read_timestamps`` (dict): pop oldest by insertion
        order (Python 3.7+ dicts).  Evicted entries lose their dedup
        skip on a future re-read (the file gets re-sent once) and
        external-edit mtime comparison (the write/patch falls back to
        a non-mtime check).  Both are graceful degradations, not bugs.
    """
    rh = task_data.get("read_history")
    if rh is not None and len(rh) > _READ_HISTORY_CAP:
        excess = len(rh) - _READ_HISTORY_CAP
        for _ in range(excess):
            try:
                rh.pop()
            except KeyError:
                break

    dedup = task_data.get("dedup")
    if dedup is not None and len(dedup) > _DEDUP_CAP:
        excess = len(dedup) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup.pop(next(iter(dedup)))
            except (StopIteration, KeyError):
                break

    dedup_hits = task_data.get("dedup_hits")
    if dedup_hits is not None and len(dedup_hits) > _DEDUP_CAP:
        excess = len(dedup_hits) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup_hits.pop(next(iter(dedup_hits)))
            except (StopIteration, KeyError):
                break

    ts = task_data.get("read_timestamps")
    if ts is not None and len(ts) > _READ_TIMESTAMPS_CAP:
        excess = len(ts) - _READ_TIMESTAMPS_CAP
        for _ in range(excess):
            try:
                ts.pop(next(iter(ts)))
            except (StopIteration, KeyError):
                break


def _is_internal_file_status_text(content: str) -> bool:
    """Return True when content looks like an internal file-tool status, not real file bytes.

    The read_file dedup status message must never be persisted as file
    content.  The obvious shape is the model echoing the message verbatim,
    but in practice it also wraps it with small framing text (a leading
    "Note:", a trailing newline + short comment, etc.) before calling
    write_file.  We treat any short-ish write whose body is dominated by
    the status message as the same class of corruption.

    Heuristic:
      * Strict equality (after strip) — the verbatim shape.
      * OR the stripped content contains the full status message AND is
        short enough that the status dominates it (<=2x the message length).
        Short, status-dominated writes can't plausibly be real files —
        legitimate docs/notes that happen to quote this internal message
        are always dramatically longer.
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if stripped == _READ_DEDUP_STATUS_MESSAGE:
        return True
    if _READ_DEDUP_STATUS_MESSAGE in stripped and \
            len(stripped) <= 2 * len(_READ_DEDUP_STATUS_MESSAGE):
        return True
    return False


def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """Get or create ShellFileOperations backed by this task's terminal toolkit env."""
    effective_task_id = task_id or "default"
    with _file_ops_lock:
        cached = _file_ops_cache.get(effective_task_id)

    env = get_or_create_active_env(effective_task_id)
    if cached is not None and getattr(cached, "env", None) is env:
        return cached

    file_ops = ShellFileOperations(env)

    with _file_ops_lock:
        cached = _file_ops_cache.get(effective_task_id)
        if cached is not None and getattr(cached, "env", None) is env:
            return cached
        _file_ops_cache[effective_task_id] = file_ops
        return file_ops


def clear_file_ops_cache(task_id: str = None):
    """Clear the file operations cache."""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()


def read_file_tool(path: str, offset: int = 1, limit: int = 500, task_id: str = "default") -> str:
    """Read a file with pagination and line numbers."""
    try:
        offset, limit = normalize_read_pagination(offset, limit)

        # ── Device path guard ─────────────────────────────────────────
        # Block paths that would hang the process (infinite output,
        # blocking on input).  Pure path check — no I/O.
        if _is_blocked_device(path):
            return json.dumps({
                "error": (
                    f"Cannot read '{path}': this is a device file that would "
                    "block or produce infinite output."
                ),
            })

        _resolved = _resolve_path_for_task(path, task_id)

        # ── Binary file guard ─────────────────────────────────────────
        # Block binary files by extension (no I/O).
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            return json.dumps({
                "error": (
                    f"Cannot read binary file '{path}' ({_ext}). "
                    "Use vision_analyze for images, or terminal to inspect binary files."
                ),
            })

        # ── Dedup check ───────────────────────────────────────────────
        # If we already read this exact (path, offset, limit) and the
        # file hasn't been modified since, return a lightweight stub
        # instead of re-sending the same content.  Saves context tokens.
        resolved_str = str(_resolved)
        dedup_key = (resolved_str, offset, limit)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0,
                "read_history": set(), "dedup": {},
                "dedup_hits": {},
            })
            # Backward-compat for pre-existing tracker entries that predate
            # dedup_hits (long-lived task or crossed an upgrade boundary).
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            cached_mtime = task_data.get("dedup", {}).get(dedup_key)

        if cached_mtime is not None:
            current_mtime = _sample_mtime_for_task(resolved_str, task_id)
            if current_mtime is not None and current_mtime == cached_mtime:
                # Count repeated stub returns so weak tool-followers that
                # ignore the "refer to earlier result" hint don't burn
                # their iteration budget in an infinite read loop.  After
                # 2 stubs for the same key we escalate to a hard block
                # mirroring the count>=4 path on real reads.
                with _read_tracker_lock:
                    hits = task_data["dedup_hits"].get(dedup_key, 0) + 1
                    task_data["dedup_hits"][dedup_key] = hits
                    _cap_read_tracker_data(task_data)

                if hits >= 2:
                    return json.dumps({
                        "error": (
                            f"BLOCKED: You have called read_file on this "
                            f"exact region {hits + 1} times and the file "
                            "has NOT changed. STOP calling read_file for "
                            "this path — the content from your earlier "
                            "read_file result in this conversation is "
                            "still current. Proceed with your task using "
                            "the information you already have."
                        ),
                        "path": path,
                        "already_read": hits + 1,
                    }, ensure_ascii=False)

                return json.dumps({
                    "status": "unchanged",
                    "message": _READ_DEDUP_STATUS_MESSAGE,
                    "path": path,
                    "dedup": True,
                    "content_returned": False,
                }, ensure_ascii=False)

        # ── Perform the read ──────────────────────────────────────────
        file_ops = _get_file_ops(task_id)
        result = file_ops.read_file(path, offset, limit)
        result_dict = result.to_dict()

        # ── Character-count guard ─────────────────────────────────────
        # We're model-agnostic so we can't count tokens; characters are
        # the best proxy we have.  If the read produced an unreasonable
        # amount of content, reject it and tell the model to narrow down.
        # Note: we check the formatted content (with line-number prefixes),
        # not the raw file size, because that's what actually enters context.
        # Check BEFORE redaction to avoid expensive regex on huge content.
        content_len = len(result.content or "")
        file_size = result_dict.get("file_size", 0)
        max_chars = _get_max_read_chars()
        if content_len > max_chars:
            total_lines = result_dict.get("total_lines", "unknown")
            return json.dumps({
                "error": (
                    f"Read produced {content_len:,} characters which exceeds "
                    f"the safety limit ({max_chars:,} chars). "
                    "Use offset and limit to read a smaller range. "
                    f"The file has {total_lines} lines total."
                ),
                "path": path,
                "total_lines": total_lines,
                "file_size": file_size,
            }, ensure_ascii=False)

        # ── Redact secrets (after guard check to skip oversized content) ──
        if result.content:
            result.content = redact_sensitive_text(result.content)
            result_dict["content"] = result.content

        # Large-file hint: if the file is big and the caller didn't ask
        # for a narrow window, nudge toward targeted reads.
        if (file_size and file_size > _LARGE_FILE_HINT_BYTES
                and limit > 200
                and result_dict.get("truncated")):
            result_dict.setdefault("_hint", (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit "
                "to keep context usage efficient."
            ))

        # Sample outside _read_tracker_lock. Non-local backends may shell out.
        _mtime_now = _sample_mtime_for_task(resolved_str, task_id)

        # ── Track for consecutive-loop detection ──────────────────────
        read_key = ("read", path, offset, limit)
        with _read_tracker_lock:
            # Ensure "dedup" / "dedup_hits" keys exist (backward compat with
            # old tracker state from pre-dedup-guard sessions).
            if "dedup" not in task_data:
                task_data["dedup"] = {}
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            # Real read succeeded — this key is no longer in a stub-loop, so
            # reset its hit counter.  (File either changed or stat failed
            # earlier and we fell through.)
            task_data["dedup_hits"].pop(dedup_key, None)
            task_data["read_history"].add((path, offset, limit))
            if task_data["last_key"] == read_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = read_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

            # Store mtime at read time for two purposes:
            # 1. Dedup: skip identical re-reads of unchanged files.
            # 2. Staleness: warn on write/patch if the file changed since
            #    the agent last read it (external edit, concurrent agent, etc.).
            if _mtime_now is None:
                task_data["dedup"].pop(dedup_key, None)
                task_data.setdefault("read_timestamps", {}).pop(resolved_str, None)
            else:
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now

            # Bound the per-task containers so a long CLI session doesn't
            # accumulate megabytes of dict/set state.  See _cap_read_tracker_data.
            _cap_read_tracker_data(task_data)

        # Cross-agent file-state registry (separate from per-task read
        # tracker above): records that THIS agent has read this path so
        # write/patch can detect sibling-subagent writes that happened
        # after our read.  Partial read when offset>1 or the read was
        # truncated (large file with more content than limit covered).
        # Outside the _read_tracker_lock so the registry's own locking
        # isn't nested under ours.
        try:
            _partial = (offset > 1) or bool(result_dict.get("truncated"))
            file_state.record_read(
                task_id,
                resolved_str,
                partial=_partial,
                mtime=_mtime_now,
            )
        except Exception:
            logger.debug("file_state.record_read failed", exc_info=True)

        if count >= 4:
            # Hard block: stop returning content to break the loop
            return json.dumps({
                "error": (
                    f"BLOCKED: You have read this exact file region {count} times in a row. "
                    "The content has NOT changed. You already have this information. "
                    "STOP re-reading and proceed with your task."
                ),
                "path": path,
                "already_read": count,
            }, ensure_ascii=False)
        elif count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))




def reset_file_dedup(task_id: str = None):
    """Clear the deduplication cache for file reads.

    Called after context compression — the original read content has been
    summarised away, so the model needs the full content if it reads the
    same file again.  Without this, reads after compression would return
    a "file unchanged" stub pointing at content that no longer exists in
    context.

    Call with a task_id to clear just that task, or without to clear all.
    """
    with _read_tracker_lock:
        if task_id:
            task_data = _read_tracker.get(task_id)
            if task_data:
                if "dedup" in task_data:
                    task_data["dedup"].clear()
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()
        else:
            for task_data in _read_tracker.values():
                if "dedup" in task_data:
                    task_data["dedup"].clear()
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()


def notify_other_tool_call(task_id: str = "default"):
    """Reset consecutive read/search counter for a task.

    Called by the tool dispatcher (model_tools.py) whenever a tool OTHER
    than read_file / search_files is executed.  This ensures we only warn
    or block on *truly consecutive* repeated reads — if the agent does
    anything else in between (write, patch, terminal, etc.) the counter
    resets and the next read is treated as fresh.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0
            # An intervening non-read tool call breaks any stub-loop in
            # progress, so clear per-key dedup hit counters too.
            if "dedup_hits" in task_data:
                task_data["dedup_hits"].clear()


def _invalidate_dedup_for_path(filepath: str, task_id: str) -> None:
    """Remove all dedup cache entries whose resolved path matches *filepath*.

    Called after write_file and patch so that a subsequent read_file on
    the same path always returns fresh content instead of a stale
    "File unchanged" stub.  The dedup cache keys are tuples of
    ``(resolved_path, offset, limit)``; we must evict **all** offset/limit
    combinations for the written path because any cached range could now
    be stale.

    Must be called with ``_read_tracker_lock`` **not** held — acquires it
    internally.
    """
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is None:
            return
        dedup = task_data.get("dedup")
        if not dedup:
            return
        # Collect keys to remove (can't mutate dict during iteration).
        stale_keys = [k for k in dedup if k[0] == resolved]
        for k in stale_keys:
            del dedup[k]


def _update_read_timestamp(filepath: str, task_id: str = "default") -> float | None:
    """Record the file's current modification time after a successful write.

    Called after write_file and patch so that consecutive edits by the
    same task don't trigger false staleness warnings — each write
    refreshes the stored timestamp to match the file's new state.

    Also invalidates the dedup cache for the written path so that
    subsequent reads return fresh content (fixes #13144). Returns the
    sampled mtime, or None when the active backend cannot provide one.
    """
    # Invalidate dedup first (before acquiring lock for timestamp update).
    _invalidate_dedup_for_path(filepath, task_id)
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return None
    current_mtime = _sample_mtime_for_task(resolved, task_id)
    if current_mtime is None:
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is not None:
            task_data.setdefault("read_timestamps", {})[resolved] = current_mtime
            _cap_read_tracker_data(task_data)
    return current_mtime


def _check_file_staleness(filepath: str, task_id: str) -> str | None:
    """Check whether a file was modified since the agent last read it.

    Returns a warning string if the file is stale (mtime changed since
    the last read_file call for this task), or None if the file is fresh
    or was never read.  Does not block — the write still proceeds.
    """
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        read_mtime = task_data.get("read_timestamps", {}).get(resolved)
    if read_mtime is None:
        return None  # File was never read — nothing to compare against
    current_mtime = _sample_mtime_for_task(resolved, task_id)
    if current_mtime is None:
        return None  # Can't stat — file may have been deleted, let write handle it
    if current_mtime != read_mtime:
        return (
            f"Warning: {filepath} was modified since you last read it "
            "(external edit or concurrent agent). The content you read may be "
            "stale. Consider re-reading the file to verify before writing."
        )
    return None


def _append_unique_path(paths: list[str], seen: set[str], path: str | None) -> None:
    if path and path not in seen:
        paths.append(path)
        seen.add(path)


def _extract_patch_paths_for_safety(patch_content: str) -> list[str]:
    """Return V4A operation paths for safety, locking, and write tracking."""
    paths: list[str] = []
    seen: set[str] = set()
    operations = []
    parse_error = None
    try:
        operations, parse_error = parse_v4a_patch(patch_content)
    except Exception as exc:
        parse_error = str(exc)

    import re as _re
    if not parse_error and operations:
        for operation in operations:
            _append_unique_path(paths, seen, operation.file_path)
            _append_unique_path(paths, seen, operation.new_path)
        for _m in _re.finditer(
            r"^\*\*\*\s+Move to:\s*(.+)$",
            patch_content,
            _re.MULTILINE,
        ):
            _append_unique_path(paths, seen, _m.group(1).strip())
        return paths

    for _m in _re.finditer(
        r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$",
        patch_content,
        _re.MULTILINE,
    ):
        _append_unique_path(paths, seen, _m.group(1).strip())
    for _m in _re.finditer(
        r"^\*\*\*\s+Move\s+File:\s*(.+?)\s*->\s*(.+)$",
        patch_content,
        _re.MULTILINE,
    ):
        _append_unique_path(paths, seen, _m.group(1).strip())
        _append_unique_path(paths, seen, _m.group(2).strip())
    for _m in _re.finditer(
        r"^\*\*\*\s+Move to:\s*(.+)$",
        patch_content,
        _re.MULTILINE,
    ):
        _append_unique_path(paths, seen, _m.group(1).strip())
    return paths


def write_file_tool(
    path: str,
    content: str,
    task_id: str = "default",
    approved_write_roots: list[str] | None = None,
) -> str:
    """Write content to a file."""
    sensitive_err = _check_sensitive_path(path, task_id)
    if sensitive_err:
        return tool_error(sensitive_err)
    if _is_internal_file_status_text(content):
        return tool_error(
            "Refusing to write internal read_file status text as file content. "
            "Re-read the file or reconstruct the intended file contents before writing."
        )
    try:
        # Resolve once for the registry lock + stale check.  Failures here
        # fall back to the legacy path — write proceeds, per-task staleness
        # check below still runs.
        try:
            _resolved = str(_resolve_path_for_task(path, task_id))
        except Exception:
            _resolved = None

        if _resolved is None:
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            with _approved_write_roots_context(file_ops, approved_write_roots):
                result = file_ops.write_file(path, content)
            result_dict = result.to_dict()
            if stale_warning:
                result_dict["_warning"] = stale_warning
            _update_read_timestamp(path, task_id)
            return json.dumps(result_dict, ensure_ascii=False)

        # Serialize the read→modify→write region per-path so concurrent
        # subagents can't interleave on the same file.  Different paths
        # remain fully parallel.
        with file_state.lock_path(_resolved):
            # Cross-agent staleness wins over per-task warning when both
            # fire — its message names the sibling subagent.
            current_mtime = _sample_mtime_for_task(_resolved, task_id)
            cross_warning = file_state.check_stale(
                task_id,
                _resolved,
                current_mtime=current_mtime,
            )
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            with _approved_write_roots_context(file_ops, approved_write_roots):
                result = file_ops.write_file(path, content)
            result_dict = result.to_dict()
            effective_warning = cross_warning or stale_warning
            if effective_warning:
                result_dict["_warning"] = effective_warning
            # Refresh stamps after the successful write so consecutive
            # writes by this task don't trigger false staleness warnings.
            if not result_dict.get("error"):
                write_mtime = _update_read_timestamp(path, task_id)
                file_state.note_write(task_id, _resolved, mtime=write_mtime)
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug("write_file expected denial: %s: %s", type(e).__name__, e)
        else:
            logger.error("write_file error: %s: %s", type(e).__name__, e, exc_info=True)
        return tool_error(str(e))


def patch_tool(mode: str = "replace", path: str = None, old_string: str = None,
               new_string: str = None, replace_all: bool = False, patch: str = None,
               task_id: str = "default",
               approved_write_roots: list[str] | None = None) -> str:
    """Patch a file using replace mode or V4A patch format."""
    # Check sensitive paths for both replace (explicit path) and V4A patch (extract paths)
    _paths_to_check = []
    if path:
        _paths_to_check.append(path)
    if mode == "patch" and patch:
        for _p in _extract_patch_paths_for_safety(patch):
            if _p not in _paths_to_check:
                _paths_to_check.append(_p)
    for _p in _paths_to_check:
        sensitive_err = _check_sensitive_path(_p, task_id)
        if sensitive_err:
            return tool_error(sensitive_err)
    try:
        # Resolve paths for locking.  Ordered + deduplicated so concurrent
        # callers lock in the same order — prevents deadlock on overlapping
        # multi-file V4A patches.
        _resolved_paths: list[str] = []
        _seen: set[str] = set()
        for _p in _paths_to_check:
            try:
                _r = str(_resolve_path_for_task(_p, task_id))
            except Exception:
                _r = None
            if _r and _r not in _seen:
                _resolved_paths.append(_r)
                _seen.add(_r)
        _resolved_paths.sort()

        # Acquire per-path locks in sorted order via ExitStack.  On single
        # path this degenerates to one lock; on empty list (unresolvable)
        # it's a no-op and execution falls through unchanged.
        from contextlib import ExitStack
        with ExitStack() as _locks:
            for _r in _resolved_paths:
                _locks.enter_context(file_state.lock_path(_r))

            # Collect warnings — cross-agent registry first (names sibling),
            # then per-task tracker as a fallback.
            stale_warnings: list[str] = []
            _path_to_resolved: dict[str, str] = {}
            for _p in _paths_to_check:
                try:
                    _r = str(_resolve_path_for_task(_p, task_id))
                except Exception:
                    _r = None
                _path_to_resolved[_p] = _r
                _current_mtime = _sample_mtime_for_task(_r, task_id) if _r else None
                _cross = (
                    file_state.check_stale(
                        task_id,
                        _r,
                        current_mtime=_current_mtime,
                    )
                    if _r
                    else None
                )
                _sw = _cross or _check_file_staleness(_p, task_id)
                if _sw:
                    stale_warnings.append(_sw)

            file_ops = _get_file_ops(task_id)

            with _approved_write_roots_context(file_ops, approved_write_roots):
                if mode == "replace":
                    if not path:
                        return tool_error("path required")
                    if old_string is None or new_string is None:
                        return tool_error("old_string and new_string required")
                    result = file_ops.patch_replace(path, old_string, new_string, replace_all)
                elif mode == "patch":
                    if not patch:
                        return tool_error("patch content required")
                    result = file_ops.patch_v4a(patch)
                else:
                    return tool_error(f"Unknown mode: {mode}")

            result_dict = result.to_dict()
            if stale_warnings:
                result_dict["_warning"] = stale_warnings[0] if len(stale_warnings) == 1 else " | ".join(stale_warnings)
            # Refresh stored timestamps for all successfully-patched paths so
            # consecutive edits by this task don't trigger false warnings.
            if not result_dict.get("error"):
                for _p in _paths_to_check:
                    _write_mtime = _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r, mtime=_write_mtime)
        # Hint when old_string not found — saves iterations where the agent
        # retries with stale content instead of re-reading the file.
        # Suppressed when patch_replace already attached a rich "Did you mean?"
        # snippet (which is strictly more useful than the generic hint).
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            if "Did you mean one of these sections?" not in str(result_dict["error"]):
                result_dict["_hint"] = (
                    "old_string not found. Use read_file to verify the current "
                    "content, or search_files to locate the text."
                )
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


def search_tool(pattern: str, target: str = "content", path: str = ".",
                file_glob: str = None, limit: int = 50, offset: int = 0,
                output_mode: str = "content", context: int = 0,
                task_id: str = "default") -> str:
    """Search for content or files."""
    try:
        offset, limit = normalize_search_pagination(offset, limit)

        # Track searches to detect *consecutive* repeated search loops.
        # Include pagination args so users can page through truncated
        # results without tripping the repeated-search guard.
        search_key = (
            "search",
            pattern,
            target,
            str(path),
            file_glob or "",
            limit,
            offset,
        )
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0, "read_history": set(),
            })
            if task_data["last_key"] == search_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = search_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            return json.dumps({
                "error": (
                    f"BLOCKED: You have run this exact search {count} times in a row. "
                    "The results have NOT changed. You already have this information. "
                    "STOP re-searching and proceed with your task."
                ),
                "pattern": pattern,
                "already_searched": count,
            }, ensure_ascii=False)

        file_ops = _get_file_ops(task_id)
        result = file_ops.search(
            pattern=pattern, path=path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context
        )
        if hasattr(result, 'matches'):
            for m in result.matches:
                if hasattr(m, 'content') and m.content:
                    m.content = redact_sensitive_text(m.content)
        result_dict = result.to_dict()

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )

        # Hint when results were truncated — explicit next offset is clearer
        # than relying on the model to infer it from total_count vs match count.
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_dict["_hint"] = (
                f"Results truncated. Use offset={next_offset} to see more, "
                "or narrow with a more specific pattern or file_glob."
            )
        result_json = json.dumps(result_dict, ensure_ascii=False)
        return result_json
    except Exception as e:
        return tool_error(str(e))

__all__ = [
    "read_file_tool",
    "write_file_tool",
    "patch_tool",
    "search_tool",
    "clear_file_ops_cache",
    "reset_file_dedup",
    "notify_other_tool_call",
]
