# Hermes Toolkit Home Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_toolkit_home()` try every configured and default toolkit-home candidate in priority order, returning the first directory that can be created.

**Architecture:** Keep all path-selection logic in `agent_tools/hermes_terminal_toolkit/paths.py`. Add regression tests that simulate mkdir failures without relying on host permissions, then simplify `get_toolkit_home()` so it builds one ordered candidate list and iterates through all candidates.

**Tech Stack:** Python 3.11, pytest, `pathlib.Path`, existing Hermes terminal toolkit path helpers.

---

## Scope And File Structure

This plan only fixes the P2 fallback semantics for toolkit home resolution. It does not change sandbox directory policy, container backends, process registry paths, or file operation safety.

Files to modify:

- `agent_tools/hermes_terminal_toolkit/paths.py`
  - Add a small private candidate-builder helper.
  - Change `get_toolkit_home()` so env-provided candidates do not short-circuit fallback.

- `tests/test_hermes_paths.py`
  - New test file covering priority order, fallback behavior, all-candidates failure, and `get_subprocess_home()` integration.

Behavior to implement:

1. Candidate order is always:
   - `HERMES_TERMINAL_TOOLKIT_HOME`
   - `$HERMES_HOME/terminal-toolkit`
   - `~/.hermes-terminal-toolkit`
   - `./.hermes-terminal-toolkit`
   - `/tmp/.hermes-terminal-toolkit`
2. Empty environment variables are ignored.
3. `get_toolkit_home()` returns the first candidate whose `mkdir(parents=True, exist_ok=True)` succeeds.
4. If all candidates fail, it raises `OSError("Unable to create a writable toolkit home directory")`.
5. `get_subprocess_home()` keeps its current public API, but when isolation is enabled it benefits from the fixed `get_toolkit_home()` fallback behavior.

---

### Task 1: Add Regression Tests For Toolkit Home Candidate Fallback

**Files:**
- Create: `tests/test_hermes_paths.py`

- [ ] **Step 1: Create the regression test file**

Create `tests/test_hermes_paths.py` with this complete content:

```python
from pathlib import Path

import pytest

from agent_tools.hermes_terminal_toolkit import paths


def test_get_toolkit_home_tries_all_candidates_in_priority_order(monkeypatch, tmp_path):
    custom_home = tmp_path / "custom-home"
    hermes_home = tmp_path / "hermes-home"
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"

    monkeypatch.setenv("HERMES_TERMINAL_TOOLKIT_HOME", str(custom_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    expected = [
        custom_home,
        hermes_home / "terminal-toolkit",
        fake_home / ".hermes-terminal-toolkit",
        fake_cwd / ".hermes-terminal-toolkit",
        Path("/tmp") / ".hermes-terminal-toolkit",
    ]
    calls = []

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True
        if self != expected[-1]:
            raise OSError("candidate unavailable")

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == expected[-1]
    assert calls == expected


def test_get_toolkit_home_starts_with_home_when_env_candidates_unset(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"
    expected_home_candidate = fake_home / ".hermes-terminal-toolkit"
    calls = []

    monkeypatch.delenv("HERMES_TERMINAL_TOOLKIT_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == expected_home_candidate
    assert calls == [expected_home_candidate]


def test_get_toolkit_home_raises_after_all_candidates_fail(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"
    calls = []

    monkeypatch.delenv("HERMES_TERMINAL_TOOLKIT_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    expected = [
        fake_home / ".hermes-terminal-toolkit",
        fake_cwd / ".hermes-terminal-toolkit",
        Path("/tmp") / ".hermes-terminal-toolkit",
    ]

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True
        raise OSError("candidate unavailable")

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    with pytest.raises(OSError, match="Unable to create a writable toolkit home directory"):
        paths.get_toolkit_home()

    assert calls == expected


def test_get_subprocess_home_uses_fallback_toolkit_home(monkeypatch, tmp_path):
    custom_file = tmp_path / "custom-home-is-a-file"
    hermes_home = tmp_path / "hermes-home"
    custom_file.write_text("not a directory")

    monkeypatch.setenv("HERMES_TERMINAL_TOOLKIT_HOME", str(custom_file))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TERMINAL_TOOLKIT_ISOLATE_HOME", "1")
    monkeypatch.delenv("HERMES_TERMINAL_TOOLKIT_SUBPROCESS_HOME", raising=False)

    subprocess_home = paths.get_subprocess_home()

    assert subprocess_home == str(hermes_home / "terminal-toolkit" / "home")
    assert Path(subprocess_home).is_dir()
```

- [ ] **Step 2: Run the new tests and verify the current bug is reproduced**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_hermes_paths.py -q
```

Expected result before implementation:

```text
FAILED tests/test_hermes_paths.py::test_get_toolkit_home_tries_all_candidates_in_priority_order
FAILED tests/test_hermes_paths.py::test_get_subprocess_home_uses_fallback_toolkit_home
```

The first failure should show that only `HERMES_TERMINAL_TOOLKIT_HOME` is attempted. The subprocess-home test should fail because the file at `HERMES_TERMINAL_TOOLKIT_HOME` blocks fallback to `$HERMES_HOME/terminal-toolkit`.

- [ ] **Step 3: Commit only if the implementation task is completed in the same working batch**

Do not commit the failing tests by themselves. Leave them in the working tree for Task 2.

---

### Task 2: Implement Ordered Candidate Fallback

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/paths.py`
- Test: `tests/test_hermes_paths.py`

- [ ] **Step 1: Add the private candidate builder**

In `agent_tools/hermes_terminal_toolkit/paths.py`, add this helper above `get_toolkit_home()`:

```python
def _toolkit_home_candidates() -> list[Path]:
    """Return toolkit-home candidates in priority order."""
    candidates: list[Path] = []

    custom = os.getenv("HERMES_TERMINAL_TOOLKIT_HOME")
    if custom:
        candidates.append(Path(os.path.expanduser(custom)))

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(os.path.expanduser(hermes_home)) / "terminal-toolkit")

    candidates.extend(
        [
            Path.home() / ".hermes-terminal-toolkit",
            Path.cwd() / ".hermes-terminal-toolkit",
            Path("/tmp") / ".hermes-terminal-toolkit",
        ]
    )
    return candidates
```

- [ ] **Step 2: Replace `get_toolkit_home()` branching with one loop over all candidates**

Replace the current `get_toolkit_home()` body in `agent_tools/hermes_terminal_toolkit/paths.py` with:

```python
def get_toolkit_home() -> Path:
    """Return the toolkit home directory used for snapshots and checkpoints."""
    for home in _toolkit_home_candidates():
        try:
            home.mkdir(parents=True, exist_ok=True)
            return home
        except OSError:
            continue
    raise OSError("Unable to create a writable toolkit home directory")
```

This preserves the existing mkdir behavior and error message, but it no longer drops fallback candidates when either environment variable is set.

- [ ] **Step 3: Run the path tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_hermes_paths.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 4: Run import-sensitive Hermes tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_hermes_active_env.py tests/test_terminal_lifecycle.py -q
```

Expected: all tests in both files pass. These tests exercise modules that import Hermes terminal toolkit code and help catch import-time side effects.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add agent_tools/hermes_terminal_toolkit/paths.py tests/test_hermes_paths.py
git commit -m "fix: fall back across toolkit home candidates"
```

---

### Task 3: Final Verification And Review

**Files:**
- Validate: `agent_tools/hermes_terminal_toolkit/paths.py`
- Validate: `tests/test_hermes_paths.py`

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_hermes_paths.py tests/test_hermes_active_env.py tests/test_terminal_lifecycle.py -q
```

Expected:

```text
passed
```

The exact count may change if adjacent tests are added later; any failure in these files must be investigated before proceeding.

- [ ] **Step 2: Run full test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Check whitespace and patch hygiene**

Run:

```bash
git diff --check HEAD~1..HEAD
```

Expected: no output and exit code `0`.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff --stat HEAD~1..HEAD
git diff HEAD~1..HEAD -- agent_tools/hermes_terminal_toolkit/paths.py tests/test_hermes_paths.py
```

Expected:

- `paths.py` has one private helper and a simplified `get_toolkit_home()` loop.
- `tests/test_hermes_paths.py` covers fallback from `HERMES_TERMINAL_TOOLKIT_HOME`, fallback from `$HERMES_HOME`, all-default failure, and `get_subprocess_home()` integration.
- No unrelated files are changed.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` and ask the reviewer to focus on:

- Whether `HERMES_TERMINAL_TOOLKIT_HOME` and `HERMES_HOME` failure cases now continue to lower-priority candidates.
- Whether empty env vars are ignored.
- Whether tests rely on host permissions or real `/tmp` writability.
- Whether `get_subprocess_home()` still respects `HERMES_TERMINAL_TOOLKIT_SUBPROCESS_HOME` taking precedence over isolation mode.

- [ ] **Step 6: Fix review findings**

If review finds Critical or Important issues, use `superpowers:receiving-code-review` before editing. Re-run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_hermes_paths.py tests/test_hermes_active_env.py tests/test_terminal_lifecycle.py -q
```

Then re-run the full suite before declaring the branch complete.

---

## Self-Review

Spec coverage:

- `HERMES_TERMINAL_TOOLKIT_HOME` fallback: covered by `test_get_toolkit_home_tries_all_candidates_in_priority_order()` and `test_get_subprocess_home_uses_fallback_toolkit_home()`.
- `$HERMES_HOME/terminal-toolkit` fallback: covered by the same tests and by implementation in `_toolkit_home_candidates()`.
- Default candidates `~`, cwd, `/tmp`: covered by candidate-order tests and all-candidates-fail test.
- "First candidate that can mkdir": covered by fake `Path.mkdir()` success/failure sequencing.
- Existing subprocess-home behavior: covered by `test_get_subprocess_home_uses_fallback_toolkit_home()`.

Placeholder scan:

- No placeholder markers, empty test instructions, or unspecified implementation steps remain.
- Every code-changing step includes concrete code.

Type consistency:

- `_toolkit_home_candidates() -> list[Path]` returns the same `Path` type consumed by `get_toolkit_home()`.
- `get_toolkit_home() -> Path` keeps its existing public return type.
- `get_subprocess_home() -> str` remains unchanged.
