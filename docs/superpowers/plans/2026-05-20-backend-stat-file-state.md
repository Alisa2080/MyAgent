# Reference Implementation Backend Stat File State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make read deduplication and cross-agent stale detection use backend file metadata for Docker, Singularity, and SSH, while explicitly degrading when backend stat is unavailable instead of silently using host `os.path.getmtime()`.

**Architecture:** Add a backend `stat_mtime()` primitive to `ShellFileOperations`, keep `file_state` as a backend-agnostic registry that accepts caller-provided mtime samples, and centralize mtime sampling in `file_tools.py`. Local paths keep host `os.path.getmtime()`, non-local paths use the active runtime backend shell, and unknown mtime is represented explicitly as `None` so sibling-writer and partial-read warnings still work.

**Tech Stack:** Python 3.11, pytest, existing terminal env abstraction, `ShellFileOperations`, POSIX shell `stat`.

---

## Scope And File Structure

This plan only covers P1 backend stat / explicit degradation for file-state coordination. It does not change backend path admission, safe-root policy, or toolkit-home fallback semantics.

Files to modify:

- `agent_tools/file_toolkit/file_operations.py`
  - Add `ShellFileOperations.stat_mtime(path: str) -> float | None`.
  - This is the only shell-backed metadata primitive needed by `file_tools.py`.

- `agent_tools/file_toolkit/file_state.py`
  - Allow explicit unknown mtime by accepting `mtime=None`.
  - Keep current host fallback only when mtime is omitted.
  - Allow `check_stale(..., current_mtime=None)` to skip external mtime drift while still checking sibling writes and partial reads.

- `agent_tools/file_toolkit/file_tools.py`
  - Import `get_backend_path_context`.
  - Add `_sample_mtime_for_task(resolved_path, task_id)`.
  - Replace direct `os.path.getmtime()` calls in read dedup, read tracking, per-task staleness, and write/patch timestamp refresh.
  - Pass explicit mtime samples to `file_state.record_read()`, `file_state.note_write()`, and `file_state.check_stale()`.

- `tests/test_file_operations_backend_stat.py`
  - New tests for `ShellFileOperations.stat_mtime()`.

- `tests/test_file_state_backend_mtime.py`
  - New tests for explicit unknown mtime semantics.

- `tests/test_file_tools_active_env.py`
  - Extend existing reference implementation/file-tools tests for backend stat sampling and update monkeypatched `file_state` call signatures where needed.

- `agent_tools/file_toolkit/README.md`
  - Document the local/backend/unknown-mtime behavior.

---

### Task 1: Add Shell Backend Mtime Primitive

**Files:**
- Modify: `agent_tools/file_toolkit/file_operations.py`
- Create: `tests/test_file_operations_backend_stat.py`

- [ ] **Step 1: Write failing tests for backend stat**

Create `tests/test_file_operations_backend_stat.py`:

```python
from agent_tools.file_toolkit.file_operations import ShellFileOperations


class RecordingEnv:
    cwd = "/workspace"

    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def execute(self, command, cwd=None, **kwargs):
        self.commands.append((command, cwd, kwargs))
        return self.responses.pop(0)


def test_stat_mtime_uses_backend_shell_and_parses_epoch_seconds():
    env = RecordingEnv([{"output": "1716200000\n", "returncode": 0}])
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("/workspace/notes.txt") == 1716200000.0

    command, cwd, kwargs = env.commands[0]
    assert "stat -c '%Y'" in command
    assert "stat -f '%m'" in command
    assert "'/workspace/notes.txt'" in command
    assert cwd == "/workspace"
    assert kwargs == {"timeout": 10}


def test_stat_mtime_returns_none_when_backend_stat_fails():
    env = RecordingEnv([{"output": "", "returncode": 1}])
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("/workspace/missing.txt") is None


def test_stat_mtime_expands_tilde_on_backend():
    env = RecordingEnv(
        [
            {"output": "/home/remote\n", "returncode": 0},
            {"output": "1716200123\n", "returncode": 0},
        ]
    )
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("~/notes.txt") == 1716200123.0

    stat_command = env.commands[1][0]
    assert "'/home/remote/notes.txt'" in stat_command
```

- [ ] **Step 2: Run the new test file and verify it fails**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_operations_backend_stat.py -q
```

Expected: FAIL with an `AttributeError` like:

```text
AttributeError: 'ShellFileOperations' object has no attribute 'stat_mtime'
```

- [ ] **Step 3: Implement `ShellFileOperations.stat_mtime()`**

Add this method in `agent_tools/file_toolkit/file_operations.py` after `_escape_shell_arg()`:

```python
    def stat_mtime(self, path: str) -> float | None:
        """Return file mtime from the terminal backend, or None if unavailable.

        GNU stat (`stat -c %Y`) covers Linux containers and most SSH hosts.
        BSD stat (`stat -f %m`) keeps the method useful for macOS SSH hosts.
        The caller decides how to degrade when no mtime can be sampled.
        """
        path = self._expand_path(path)
        quoted = self._escape_shell_arg(path)
        result = self._exec(
            f"stat -c '%Y' {quoted} 2>/dev/null || stat -f '%m' {quoted} 2>/dev/null",
            timeout=10,
        )
        if result.exit_code != 0:
            return None
        for line in result.stdout.splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                return float(value)
            except ValueError:
                return None
        return None
```

- [ ] **Step 4: Run the backend stat tests and verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_operations_backend_stat.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add agent_tools/file_toolkit/file_operations.py tests/test_file_operations_backend_stat.py
git commit -m "feat: add backend file mtime stat"
```

---

### Task 2: Teach FileStateRegistry Explicit Unknown Mtime

**Files:**
- Modify: `agent_tools/file_toolkit/file_state.py`
- Create: `tests/test_file_state_backend_mtime.py`

- [ ] **Step 1: Write failing tests for explicit unknown mtime**

Create `tests/test_file_state_backend_mtime.py`:

```python
from agent_tools.file_toolkit.file_state import FileStateRegistry


def test_explicit_unknown_mtime_records_sibling_writer_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, mtime=None)
    registry.note_write("worker", path, mtime=None)

    warning = registry.check_stale("parent", path, current_mtime=None)

    assert warning is not None
    assert "modified by sibling subagent 'worker'" in warning
    assert path in warning


def test_explicit_unknown_mtime_preserves_partial_read_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, partial=True, mtime=None)

    warning = registry.check_stale("parent", path, current_mtime=None)

    assert warning is not None
    assert "partial view" in warning


def test_explicit_unknown_mtime_skips_external_mtime_drift_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, mtime=None)

    assert registry.check_stale("parent", path, current_mtime=None) is None


def test_omitted_mtime_keeps_existing_host_getmtime_degradation(monkeypatch):
    registry = FileStateRegistry()
    path = "/workspace/not-on-host.txt"

    def missing(_path):
        raise OSError("not on host")

    monkeypatch.setattr("agent_tools.file_toolkit.file_state.os.path.getmtime", missing)

    registry.record_read("parent", path)

    assert registry.known_reads("parent") == []
```

- [ ] **Step 2: Run the new file-state tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_state_backend_mtime.py -q
```

Expected: FAIL with a `TypeError` like:

```text
TypeError: record_read() got an unexpected keyword argument 'mtime'
```

or:

```text
TypeError: check_stale() got an unexpected keyword argument 'current_mtime'
```

- [ ] **Step 3: Update file-state stamp type and sentinel**

In `agent_tools/file_toolkit/file_state.py`, replace the public stamp comment and add a sentinel near the constants:

```python
# ── Public stamp type ────────────────────────────────────────────────
# (mtime, read_ts, partial). mtime is None when a non-host backend could
# not provide metadata. Unknown mtime disables external-drift comparison,
# but read timestamps still support sibling-writer and partial-read checks.
ReadStamp = Tuple[Optional[float], float, bool]
_MTIME_NOT_PROVIDED = object()
```

- [ ] **Step 4: Update `record_read()` to distinguish omitted mtime from explicit unknown mtime**

Replace `FileStateRegistry.record_read()` with:

```python
    def record_read(
        self,
        task_id: str,
        resolved: str,
        *,
        partial: bool = False,
        mtime=_MTIME_NOT_PROVIDED,
    ) -> None:
        if _disabled():
            return
        if mtime is _MTIME_NOT_PROVIDED:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        stored_mtime = None if mtime is None else float(mtime)
        now = time.time()
        with self._state_lock:
            agent_reads = self._reads[task_id]
            agent_reads[resolved] = (stored_mtime, now, bool(partial))
            _cap_dict(agent_reads, _MAX_PATHS_PER_AGENT)
```

- [ ] **Step 5: Update `note_write()` to store last writer when mtime is explicitly unknown**

Replace `FileStateRegistry.note_write()` with:

```python
    def note_write(
        self,
        task_id: str,
        resolved: str,
        *,
        mtime=_MTIME_NOT_PROVIDED,
    ) -> None:
        """Record a successful write.

        Updates the global last-writer map AND this agent's own read stamp
        (a write is an implicit read — the agent now knows the current
        content).
        """
        if _disabled():
            return
        if mtime is _MTIME_NOT_PROVIDED:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        stored_mtime = None if mtime is None else float(mtime)
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _cap_dict(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._reads[task_id][resolved] = (stored_mtime, now, False)
            _cap_dict(self._reads[task_id], _MAX_PATHS_PER_AGENT)
```

- [ ] **Step 6: Update `check_stale()` to accept caller-provided current mtime**

Replace `FileStateRegistry.check_stale()` with:

```python
    def check_stale(
        self,
        task_id: str,
        resolved: str,
        *,
        current_mtime=_MTIME_NOT_PROVIDED,
    ) -> Optional[str]:
        """Return a model-facing warning if this write would be stale.

        Unknown mtime (`current_mtime=None`) is an explicit degradation used
        for remote backends when shell stat is unavailable. In that mode,
        sibling-writer and partial-read checks still run, while external
        mtime drift comparison is skipped.
        """
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)

        if stamp is None and last_writer is None:
            return None

        if current_mtime is _MTIME_NOT_PROVIDED:
            try:
                current_mtime = os.path.getmtime(resolved)
            except OSError:
                return None
        else:
            current_mtime = None if current_mtime is None else float(current_mtime)

        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} but this agent never read it. "
                        "Read the file before writing to avoid overwriting "
                        "the sibling's changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this agent's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing."
                    )

        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if (
                current_mtime is not None
                and read_mtime is not None
                and current_mtime != read_mtime
            ):
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing."
                )
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Re-read the whole file before "
                    "overwriting it."
                )

        if stamp is None:
            return (
                f"{resolved} was not read by this agent. "
                "Read the file first so you can write an informed edit."
            )

        return None
```

- [ ] **Step 7: Update module-level wrappers to pass through mtime values**

Replace the wrapper functions near the bottom of `agent_tools/file_toolkit/file_state.py` with:

```python
def record_read(
    task_id: str,
    resolved_or_path: str | Path,
    *,
    partial: bool = False,
    mtime=_MTIME_NOT_PROVIDED,
) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial, mtime=mtime)


def note_write(
    task_id: str,
    resolved_or_path: str | Path,
    *,
    mtime=_MTIME_NOT_PROVIDED,
) -> None:
    _registry.note_write(task_id, str(resolved_or_path), mtime=mtime)


def check_stale(
    task_id: str,
    resolved_or_path: str | Path,
    *,
    current_mtime=_MTIME_NOT_PROVIDED,
) -> Optional[str]:
    return _registry.check_stale(
        task_id,
        str(resolved_or_path),
        current_mtime=current_mtime,
    )
```

- [ ] **Step 8: Run file-state tests and verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_state_backend_mtime.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 9: Run existing focused tests that touch file_state monkeypatches**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_tools_active_env.py -q
```

Expected: some tests may fail because monkeypatched `check_stale` or `note_write` lambdas accept only two positional args. Those failures are expected before Task 3 updates call sites and tests.

- [ ] **Step 10: Commit Task 2**

Run:

```bash
git add agent_tools/file_toolkit/file_state.py tests/test_file_state_backend_mtime.py
git commit -m "feat: allow unknown backend mtime in file state"
```

---

### Task 3: Wire Backend Stat Into File Tools

**Files:**
- Modify: `agent_tools/file_toolkit/file_tools.py`
- Modify: `tests/test_file_tools_active_env.py`

- [ ] **Step 1: Write failing tests for backend stat usage in file tools**

Append these tests to `tests/test_file_tools_active_env.py`:

```python
def test_read_file_tool_uses_backend_mtime_for_non_local_dedup(monkeypatch):
    from agent_tools.file_toolkit.result_models import ReadResult
    from agent_tools.terminal_toolkit import terminal_tool

    class DockerEnv:
        cwd = "/workspace"
        _backend_env_type = "docker"
        _backend_configured_cwd = "/workspace"

    class FakeFileOps:
        def __init__(self):
            self.reads = 0
            self.stats = []

        def stat_mtime(self, path):
            self.stats.append(path)
            return 1716200000.0

        def read_file(self, path, offset=1, limit=500):
            self.reads += 1
            return ReadResult(
                content="     1|hello",
                total_lines=1,
                lines_read=1,
            )

    fake_ops = FakeFileOps()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/workspace", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: DockerEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    first = json.loads(file_tools.read_file_tool("notes.txt", task_id="docker-task"))
    second = json.loads(file_tools.read_file_tool("notes.txt", task_id="docker-task"))

    assert "error" not in first
    assert second["message"].startswith("File unchanged since last read")
    assert fake_ops.reads == 1
    assert fake_ops.stats == ["/workspace/notes.txt", "/workspace/notes.txt"]


def test_read_file_tool_records_unknown_backend_mtime_explicitly(monkeypatch):
    from agent_tools.file_toolkit.result_models import ReadResult
    from agent_tools.terminal_toolkit import terminal_tool

    class SSHEnv:
        cwd = "/home/remote/project"
        _backend_env_type = "ssh"
        _backend_configured_cwd = "/home/remote/project"

    class FakeFileOps:
        def stat_mtime(self, path):
            return None

        def read_file(self, path, offset=1, limit=500):
            return ReadResult(content="     1|hello", total_lines=1, lines_read=1)

    recorded = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "/home/remote/project", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: SSHEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: FakeFileOps())
    monkeypatch.setattr(
        file_tools.file_state,
        "record_read",
        lambda task_id, path, *, partial=False, mtime=None: recorded.append(
            (task_id, path, partial, mtime)
        ),
    )
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    payload = json.loads(file_tools.read_file_tool("notes.txt", task_id="ssh-task"))

    assert "error" not in payload
    assert recorded == [("ssh-task", "/home/remote/project/notes.txt", False, None)]


def test_write_file_tool_passes_backend_mtime_to_stale_and_note_write(monkeypatch):
    from agent_tools.file_toolkit.result_models import WriteResult
    from agent_tools.terminal_toolkit import terminal_tool

    class DockerEnv:
        cwd = "/workspace"
        _backend_env_type = "docker"
        _backend_configured_cwd = "/workspace"

    class FakeFileOps:
        def __init__(self):
            self.stats = []

        def stat_mtime(self, path):
            self.stats.append(path)
            return 1716200300.0

        def write_file(self, path, content):
            return WriteResult(bytes_written=len(content))

    class NoopLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_ops = FakeFileOps()
    stale_checks = []
    note_writes = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/workspace", "host_cwd": None},
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: DockerEnv())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: fake_ops)
    monkeypatch.setattr(file_tools.file_state, "lock_path", lambda path: NoopLock())
    monkeypatch.setattr(
        file_tools.file_state,
        "check_stale",
        lambda task_id, path, *, current_mtime=None: stale_checks.append(
            (task_id, path, current_mtime)
        )
        or None,
    )
    monkeypatch.setattr(
        file_tools.file_state,
        "note_write",
        lambda task_id, path, *, mtime=None: note_writes.append((task_id, path, mtime)),
    )
    monkeypatch.setattr(
        file_tools.os.path,
        "getmtime",
        lambda path: (_ for _ in ()).throw(AssertionError("host getmtime used")),
    )

    payload = json.loads(file_tools.write_file_tool("notes.txt", "hello", task_id="docker-task"))

    assert "error" not in payload
    assert stale_checks == [("docker-task", "/workspace/notes.txt", 1716200300.0)]
    assert note_writes == [("docker-task", "/workspace/notes.txt", 1716200300.0)]
    assert fake_ops.stats == ["/workspace/notes.txt", "/workspace/notes.txt"]
```

- [ ] **Step 2: Run the new file-tools tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_tools_active_env.py::test_read_file_tool_uses_backend_mtime_for_non_local_dedup tests/test_file_tools_active_env.py::test_read_file_tool_records_unknown_backend_mtime_explicitly tests/test_file_tools_active_env.py::test_write_file_tool_passes_backend_mtime_to_stale_and_note_write -q
```

Expected: FAIL because `file_tools.py` still calls host `os.path.getmtime()` and does not pass mtime keywords to `file_state`.

- [ ] **Step 3: Import backend context in `file_tools.py`**

At the top of `agent_tools/file_toolkit/file_tools.py`, change:

```python
from agent_tools.file_toolkit.backend_paths import resolve_path_for_policy
```

to:

```python
from agent_tools.file_toolkit.backend_paths import (
    get_backend_path_context,
    resolve_path_for_policy,
)
```

- [ ] **Step 4: Add centralized mtime sampler**

Add this helper after `_resolve_path_for_task()` in `agent_tools/file_toolkit/file_tools.py`:

```python
def _sample_mtime_for_task(resolved_path: str, task_id: str = "default") -> float | None:
    """Sample mtime from the correct filesystem for this task.

    Local environments use host os.path.getmtime. Docker, Singularity, and SSH
    use the active runtime backend shell. A None result is an explicit
    degradation: dedup and external-drift checks are skipped, but file_state
    still records reads/writes for sibling-writer coordination.
    """
    effective_task_id = task_id or "default"
    try:
        ctx = get_backend_path_context(effective_task_id)
    except Exception:
        logger.debug("Failed to resolve backend context for mtime sampling", exc_info=True)
        ctx = None

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
```

- [ ] **Step 5: Replace read dedup host mtime check**

In `read_file_tool()`, replace:

```python
        if cached_mtime is not None:
            try:
                current_mtime = os.path.getmtime(resolved_str)
                if current_mtime == cached_mtime:
                    # Count repeated stub returns so weak tool-followers that
```

with:

```python
        current_mtime = _sample_mtime_for_task(resolved_str, task_id)
        if cached_mtime is not None and current_mtime is not None:
            if current_mtime == cached_mtime:
                # Count repeated stub returns so weak tool-followers that
```

Then remove the matching `except OSError: pass` block from the old `try` statement and unindent the existing stub-return body one level so it remains under `if current_mtime == cached_mtime:`.

- [ ] **Step 6: Replace read success mtime storage and file_state record**

In `read_file_tool()`, replace:

```python
            try:
                _mtime_now = os.path.getmtime(resolved_str)
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
            except OSError:
                pass  # Can't stat — skip tracking for this entry
```

with:

```python
            _mtime_now = _sample_mtime_for_task(resolved_str, task_id)
            if _mtime_now is None:
                task_data["dedup"].pop(dedup_key, None)
                task_data.setdefault("read_timestamps", {}).pop(resolved_str, None)
            else:
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
```

Then replace:

```python
            file_state.record_read(task_id, resolved_str, partial=_partial)
```

with:

```python
            file_state.record_read(
                task_id,
                resolved_str,
                partial=_partial,
                mtime=_mtime_now,
            )
```

- [ ] **Step 7: Return mtime from `_update_read_timestamp()`**

Change the signature:

```python
def _update_read_timestamp(filepath: str, task_id: str = "default") -> None:
```

to:

```python
def _update_read_timestamp(filepath: str, task_id: str = "default") -> float | None:
```

Replace its body with:

```python
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
```

- [ ] **Step 8: Replace per-task staleness host mtime**

In `_check_file_staleness()`, replace:

```python
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None  # Can't stat — file may have been deleted, let write handle it
```

with:

```python
    current_mtime = _sample_mtime_for_task(resolved, task_id)
    if current_mtime is None:
        return None
```

- [ ] **Step 9: Pass backend current mtime to cross-agent stale checks in write**

In `write_file_tool()`, inside the `with file_state.lock_path(_resolved):` block, replace:

```python
            cross_warning = file_state.check_stale(task_id, _resolved)
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(path, content)
```

with:

```python
            current_mtime = _sample_mtime_for_task(_resolved, task_id)
            cross_warning = file_state.check_stale(
                task_id,
                _resolved,
                current_mtime=current_mtime,
            )
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(path, content)
```

Then replace:

```python
            _update_read_timestamp(path, task_id)
            if not result_dict.get("error"):
                file_state.note_write(task_id, _resolved)
```

with:

```python
            write_mtime = _update_read_timestamp(path, task_id)
            if not result_dict.get("error"):
                file_state.note_write(task_id, _resolved, mtime=write_mtime)
```

- [ ] **Step 10: Pass backend current mtime and write mtime in patch**

In `patch_tool()`, replace:

```python
                _cross = file_state.check_stale(task_id, _r) if _r else None
```

with:

```python
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
```

Then replace:

```python
                    _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r)
```

with:

```python
                    _write_mtime = _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r, mtime=_write_mtime)
```

- [ ] **Step 11: Update existing monkeypatched file-state tests**

In `tests/test_file_tools_active_env.py`, update any monkeypatch lambdas for `check_stale` and `note_write` so they accept keyword-only mtime values:

```python
lambda task_id, path, *, current_mtime=None: stale_checked.append((task_id, path)) or None
```

and:

```python
lambda task_id, path, *, mtime=None: note_writes.append((task_id, path))
```

Keep existing assertions unchanged unless the test is explicitly checking mtime values.

- [ ] **Step 12: Run the new file-tools tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_tools_active_env.py::test_read_file_tool_uses_backend_mtime_for_non_local_dedup tests/test_file_tools_active_env.py::test_read_file_tool_records_unknown_backend_mtime_explicitly tests/test_file_tools_active_env.py::test_write_file_tool_passes_backend_mtime_to_stale_and_note_write -q
```

Expected:

```text
3 passed
```

- [ ] **Step 13: Run all file-tools reference implementation tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_tools_active_env.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 14: Commit Task 3**

Run:

```bash
git add agent_tools/file_toolkit/file_tools.py tests/test_file_tools_active_env.py
git commit -m "fix: sample file mtime through active backend"
```

---

### Task 4: Document Backend Stat Degradation

**Files:**
- Modify: `agent_tools/file_toolkit/README.md`

- [ ] **Step 1: Add documentation for mtime behavior**

Add this section near the existing backend-aware path policy documentation in `agent_tools/file_toolkit/README.md`:

```markdown
### Backend mtime sampling and stale detection

Read deduplication and stale-write warnings sample file modification time from
the filesystem that owns the path:

- local backend: host `os.path.getmtime()`
- Docker, Singularity, SSH: backend shell `stat` through `ShellFileOperations`

When backend `stat` is unavailable, the file tools explicitly store an unknown
mtime (`None`). Unknown mtime disables exact external-drift comparison and
read dedup for that path, but it does not disable cross-agent coordination:
the file-state registry still records reads, successful writes, sibling-writer
timestamps, and partial-read warnings.
```

- [ ] **Step 2: Verify the docs mention both backend stat and unknown mtime**

Run:

```bash
rg -n "Backend mtime sampling|unknown mtime|ShellFileOperations" agent_tools/file_toolkit/README.md
```

Expected output includes all three search terms.

- [ ] **Step 3: Commit Task 4**

Run:

```bash
git add agent_tools/file_toolkit/README.md
git commit -m "docs: describe backend mtime degradation"
```

---

### Task 5: Final Verification And Review

**Files:**
- No new source files.
- Validate all files changed by Tasks 1-4.

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_file_operations_backend_stat.py tests/test_file_state_backend_mtime.py tests/test_file_tools_active_env.py tests/test_backend_path_policy.py tests/test_file_tools_runtime_task_id.py -q
```

Expected:

```text
passed
```

The exact count may vary if unrelated tests are added before implementation; any failure in these files must be investigated before proceeding.

- [ ] **Step 2: Run full suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Check formatting-sensitive whitespace**

Run:

```bash
git diff --check HEAD~4..HEAD
```

Expected: no output and exit code `0`.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- agent_tools/file_toolkit/file_operations.py agent_tools/file_toolkit/file_state.py agent_tools/file_toolkit/file_tools.py
```

Expected:

- `file_operations.py` only adds `stat_mtime()`.
- `file_state.py` stores explicit unknown mtime without changing lock or cap behavior.
- `file_tools.py` has no remaining `os.path.getmtime()` calls except inside `_sample_mtime_for_task()`.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` and ask the reviewer to focus on:

- non-local backends never falling back to host mtime
- unknown mtime still preserving sibling-writer warnings
- no circular import between `file_tools.py`, `file_state.py`, and `file_operations.py`
- test fakes accurately exercising Docker/SSH behavior

- [ ] **Step 6: Fix review findings**

If the review finds issues, use `superpowers:receiving-code-review` before changing code. Re-run the focused tests from Step 1 after each fix commit.

---

## Self-Review

Spec coverage:

- P1 backend stat abstraction: Task 1 adds `ShellFileOperations.stat_mtime()`.
- P1 file_state mtime coordination: Task 2 makes mtime caller-provided and supports explicit unknown mtime.
- P1 read dedup/stale checks on remote backend: Task 3 wires backend mtime into read dedup, per-task stale warning, cross-agent stale warning, and write tracking.
- Explicit degradation: Task 2 and Task 3 represent unavailable backend stat as `None`; docs in Task 4 describe the reduced guarantees.

Placeholder scan:

- No placeholder markers, empty "add tests" instructions, or unspecified implementation steps remain.
- Every code-changing step includes concrete code.

Type consistency:

- `ShellFileOperations.stat_mtime(path: str) -> float | None` is used by `_sample_mtime_for_task()`.
- `file_state.record_read(..., mtime=...)`, `note_write(..., mtime=...)`, and `check_stale(..., current_mtime=...)` match the registry and module wrapper signatures.
- Unknown mtime is consistently represented as `None`.
