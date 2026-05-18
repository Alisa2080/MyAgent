# Agent Tools Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `agent_tools/` into clear public tool, shared support, internal toolkit, and vendored Hermes areas without breaking existing LangChain tool registration or tests.

**Architecture:** Use a compatibility-first migration. Keep high-risk internal packages (`file_toolkit/` and `hermes_terminal_toolkit/`) in place initially, add package-level documentation and stable public import facades, then migrate first-party top-level modules into clearer subpackages with re-export shims. Generated caches and accidental copy files are cleaned only after tests prove they are unused.

**Tech Stack:** Python 3.11, LangChain tool decorators, pytest via `/home/miku/miniforge3/envs/langchain/bin/python`, Git-aware file moves with `git mv`.

---

## Current Findings

`agent_tools/` currently mixes four responsibilities at one level:

- Public LangChain tools: `file_tools.py`, `terminal_tools.py`, `web.py`, `memory_tools.py`, `skills.py`, `skill_manage.py`.
- Shared first-party helpers: `common.py`, `file_policy.py`, `tool_output.py`, package `__init__.py`, compatibility `general.py`.
- Internal file implementation package: `file_toolkit/`, with large implementation files such as `file_operations.py` and `file_tools.py`.
- Vendored/imported Hermes terminal toolkit: `hermes_terminal_toolkit/`, including environments, process registry, terminal primitives, and toolkit-specific LangChain adapters.

Noise and risk discovered during inventory:

- `agent_tools/__pycache__/`, `agent_tools/file_toolkit/__pycache__/`, and `agent_tools/hermes_terminal_toolkit/__pycache__/` are generated artifacts and should not be part of source organization.
- `agent_tools/hermes_terminal_toolkit/process_registry copy.py` is an untracked duplicate copy file and should be removed only after confirming no imports reference it.
- `agent_tools/general.py` is already a compatibility export layer and can become the migration compatibility boundary.
- `agent_tools/file_toolkit/` and `agent_tools/hermes_terminal_toolkit/` use many absolute imports under their current package names; moving them in the same pass would create unnecessary risk.
- Current worktree is dirty from terminal/process work. Do not begin this reorganization until that work is committed or isolated in a new worktree.

## Target Layout

Final target for this plan:

```text
agent_tools/
  __init__.py
  README.md
  public/
    __init__.py
    files.py
    terminal.py
    web.py
    memory.py
    skills.py
  shared/
    __init__.py
    common.py
    file_policy.py
    tool_output.py
  file_toolkit/
    README.md
    ...
  hermes_terminal_toolkit/
    README.md
    ...
  file_tools.py          # compatibility shim
  terminal_tools.py      # compatibility shim
  web.py                 # compatibility shim
  memory_tools.py        # compatibility shim
  skills.py              # compatibility shim
  skill_manage.py        # compatibility shim
  common.py              # compatibility shim
  file_policy.py         # compatibility shim
  tool_output.py         # compatibility shim
  general.py             # compatibility shim
```

Boundary rules after migration:

- New code imports public LangChain tools from `agent_tools.public.*`.
- New code imports shared helpers from `agent_tools.shared.*`.
- Compatibility shims preserve existing imports such as `from agent_tools.file_tools import read_file`.
- `file_toolkit/` stays internal and is imported only by `agent_tools.public.files` or tests for low-level file behavior.
- `hermes_terminal_toolkit/` is treated as vendored toolkit code. Project-native LangChain wrappers live in `agent_tools.public.terminal`, not inside Hermes toolkit.
- Generated caches and duplicate files are not source files.

---

### Task 0: Preparation and Safety Baseline

**Files:**
- No source edits.

- [ ] **Step 1: Confirm dirty worktree and branch**

Run:

```bash
git status --short --branch
```

Expected: shows the current branch and any pre-existing changes. If terminal/process work is still uncommitted, stop and either commit it or create an isolated worktree before reorganizing files.

- [ ] **Step 2: Create isolated branch/worktree**

Run one of these, depending on current workflow:

```bash
git switch -c agent-tools-organization
```

or, if the current branch must remain untouched:

```bash
git worktree add ../langchain-agent-tools-organization -b agent-tools-organization
```

Expected: an isolated branch/worktree where file moves will not mix with terminal/process feature changes.

- [ ] **Step 3: Run baseline tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass before file moves. If they fail, record the failing tests and fix or defer this cleanup until the branch is green.

- [ ] **Step 4: Confirm accidental copy file is unused**

Run:

```bash
rg -n "process_registry copy|process_registry\\ copy" .
```

Expected: no source imports or references. If any references exist, stop and investigate before cleanup.

- [ ] **Step 5: Commit baseline if needed**

If the branch contains only preparation docs or no changes, do not commit. If you had to add a baseline note, commit:

```bash
git add docs/superpowers/plans/2026-05-18-agent-tools-organization.md
git commit -m "docs: add agent tools organization plan"
```

---

### Task 1: Document Current and Target Package Boundaries

**Files:**
- Create: `agent_tools/README.md`
- Modify: `README.md`
- Test: no dedicated Python test; docs plus import smoke in Task 2.

- [ ] **Step 1: Create `agent_tools/README.md`**

Create `agent_tools/README.md`:

```markdown
# Agent Tools Package

`agent_tools` contains LangChain-facing tools plus the implementation packages those tools wrap.

## Public Tool Facades

New runtime code should import LangChain tools from `agent_tools.public`:

- `agent_tools.public.files`: `list_directory`, `read_file`, `write_file`, `patch`, `search_files`, `file_info`
- `agent_tools.public.terminal`: `terminal`, `process`
- `agent_tools.public.web`: `web_search`, `web_fetch`
- `agent_tools.public.memory`: `memory_manage`
- `agent_tools.public.skills`: `skills_list`, `skill_view`, `skill_manage`

## Shared Helpers

First-party helpers live under `agent_tools.shared`:

- `common`: truncation, path metadata, and common formatting helpers.
- `file_policy`: workspace path policy for file tools.
- `tool_output`: normalized JSON success/error responses.

## Internal Toolkits

- `file_toolkit/` is the internal implementation for workspace file operations. It is not a LangChain tool registration boundary.
- `hermes_terminal_toolkit/` is the imported Hermes terminal toolkit. Project-native LangChain wrappers live in `agent_tools.public.terminal`.

## Compatibility Imports

Top-level modules such as `agent_tools.file_tools`, `agent_tools.terminal_tools`, and `agent_tools.web` are kept as compatibility shims during migration. Do not add new implementation logic to those files.
```

- [ ] **Step 2: Update root README import rules**

In `README.md`, replace the `agent_tools/` runtime layout bullets with:

```markdown
- `agent_tools/`: LangChain-facing tools plus internal tool implementation packages.
  - `public/`: preferred import location for LangChain tools exposed to agents.
  - `shared/`: first-party helper modules used by tool wrappers.
  - `file_toolkit/`: internal workspace file operation implementation.
  - `hermes_terminal_toolkit/`: imported Hermes terminal toolkit implementation.
  - Top-level modules such as `file_tools.py` and `terminal_tools.py` are compatibility shims during migration.
```

- [ ] **Step 3: Run docs sanity scan**

Run:

```bash
rg -n "shell\\.py|process_tools\\.py|agent_tools/general.py" README.md agent_tools/README.md
```

Expected: no stale claim that `shell.py` or `process_tools.py` exists. `agent_tools/general.py` may appear only as a compatibility module if intentionally documented.

- [ ] **Step 4: Commit**

```bash
git add README.md agent_tools/README.md
git commit -m "docs: define agent tools package boundaries"
```

---

### Task 2: Add Public Facade Package Without Moving Implementations

**Files:**
- Create: `agent_tools/public/__init__.py`
- Create: `agent_tools/public/files.py`
- Create: `agent_tools/public/terminal.py`
- Create: `agent_tools/public/web.py`
- Create: `agent_tools/public/memory.py`
- Create: `agent_tools/public/skills.py`
- Create: `tests/test_agent_tools_public_imports.py`

- [ ] **Step 1: Write public import tests**

Create `tests/test_agent_tools_public_imports.py`:

```python
def test_public_files_exports_existing_tool_objects():
    from agent_tools.file_tools import file_info, list_directory, patch, read_file, search_files, write_file
    from agent_tools.public.files import (
        file_info as public_file_info,
        list_directory as public_list_directory,
        patch as public_patch,
        read_file as public_read_file,
        search_files as public_search_files,
        write_file as public_write_file,
    )

    assert public_list_directory is list_directory
    assert public_read_file is read_file
    assert public_write_file is write_file
    assert public_patch is patch
    assert public_search_files is search_files
    assert public_file_info is file_info


def test_public_terminal_exports_existing_tool_objects():
    from agent_tools.public.terminal import process as public_process
    from agent_tools.public.terminal import terminal as public_terminal
    from agent_tools.terminal_tools import process, terminal

    assert public_terminal is terminal
    assert public_process is process


def test_public_web_memory_and_skills_exports_existing_tool_objects():
    from agent_tools.memory_tools import memory_manage
    from agent_tools.public.memory import memory_manage as public_memory_manage
    from agent_tools.public.skills import skill_manage as public_skill_manage
    from agent_tools.public.skills import skill_view as public_skill_view
    from agent_tools.public.skills import skills_list as public_skills_list
    from agent_tools.public.web import web_fetch as public_web_fetch
    from agent_tools.public.web import web_search as public_web_search
    from agent_tools.skill_manage import skill_manage
    from agent_tools.skills import skill_view, skills_list
    from agent_tools.web import web_fetch, web_search

    assert public_memory_manage is memory_manage
    assert public_web_search is web_search
    assert public_web_fetch is web_fetch
    assert public_skills_list is skills_list
    assert public_skill_view is skill_view
    assert public_skill_manage is skill_manage
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py -v
```

Expected: fails because `agent_tools.public` does not exist.

- [ ] **Step 3: Add `agent_tools/public/__init__.py`**

Create `agent_tools/public/__init__.py`:

```python
"""Preferred public import surface for LangChain-facing tools."""

from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.memory import memory_manage
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_fetch, web_search

__all__ = [
    "file_info",
    "list_directory",
    "memory_manage",
    "patch",
    "process",
    "read_file",
    "search_files",
    "skill_manage",
    "skill_view",
    "skills_list",
    "terminal",
    "web_fetch",
    "web_search",
    "write_file",
]
```

- [ ] **Step 4: Add facade modules**

Create `agent_tools/public/files.py`:

```python
"""LangChain-facing workspace file tools."""

from agent_tools.file_tools import file_info, list_directory, patch, read_file, search_files, write_file

__all__ = [
    "file_info",
    "list_directory",
    "patch",
    "read_file",
    "search_files",
    "write_file",
]
```

Create `agent_tools/public/terminal.py`:

```python
"""LangChain-facing Hermes terminal and process tools."""

from agent_tools.terminal_tools import process, terminal

__all__ = ["process", "terminal"]
```

Create `agent_tools/public/web.py`:

```python
"""LangChain-facing web tools."""

from agent_tools.web import web_fetch, web_search

__all__ = ["web_fetch", "web_search"]
```

Create `agent_tools/public/memory.py`:

```python
"""LangChain-facing memory tools."""

from agent_tools.memory_tools import memory_manage

__all__ = ["memory_manage"]
```

Create `agent_tools/public/skills.py`:

```python
"""LangChain-facing skill discovery and management tools."""

from agent_tools.skill_manage import skill_manage
from agent_tools.skills import skill_view, skills_list

__all__ = ["skill_manage", "skill_view", "skills_list"]
```

- [ ] **Step 5: Run public import tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public tests/test_agent_tools_public_imports.py
git commit -m "feat: add agent tools public facade"
```

---

### Task 3: Add Shared Helper Facade Package Without Moving Implementations

**Files:**
- Create: `agent_tools/shared/__init__.py`
- Create: `agent_tools/shared/common.py`
- Create: `agent_tools/shared/file_policy.py`
- Create: `agent_tools/shared/tool_output.py`
- Modify: `tests/test_agent_tools_public_imports.py`

- [ ] **Step 1: Add shared facade tests**

Append to `tests/test_agent_tools_public_imports.py`:

```python
def test_shared_common_exports_existing_helpers():
    from agent_tools.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path, truncate
    from agent_tools.shared.common import (
        DEFAULT_EXCLUDE_DIRS as public_default_exclude_dirs,
        path_info as public_path_info,
        relative_path as public_relative_path,
        truncate as public_truncate,
    )

    assert public_default_exclude_dirs is DEFAULT_EXCLUDE_DIRS
    assert public_path_info is path_info
    assert public_relative_path is relative_path
    assert public_truncate is truncate


def test_shared_policy_and_output_exports_existing_helpers():
    from agent_tools.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path
    from agent_tools.shared.file_policy import (
        ensure_patch_paths as public_ensure_patch_paths,
        ensure_read_allowed as public_ensure_read_allowed,
        ensure_workspace_path as public_ensure_workspace_path,
    )
    from agent_tools.shared.tool_output import tool_error as public_tool_error
    from agent_tools.shared.tool_output import tool_ok as public_tool_ok
    from agent_tools.tool_output import tool_error, tool_ok

    assert public_ensure_patch_paths is ensure_patch_paths
    assert public_ensure_read_allowed is ensure_read_allowed
    assert public_ensure_workspace_path is ensure_workspace_path
    assert public_tool_error is tool_error
    assert public_tool_ok is tool_ok
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py -v
```

Expected: fails because `agent_tools.shared` does not exist.

- [ ] **Step 3: Add shared facade modules**

Create `agent_tools/shared/__init__.py`:

```python
"""Preferred public import surface for first-party tool helper modules."""
```

Create `agent_tools/shared/common.py`:

```python
"""Compatibility facade for common helper functions."""

from agent_tools.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path, truncate

__all__ = ["DEFAULT_EXCLUDE_DIRS", "path_info", "relative_path", "truncate"]
```

Create `agent_tools/shared/file_policy.py`:

```python
"""Compatibility facade for workspace file policy helpers."""

from agent_tools.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path

__all__ = ["ensure_patch_paths", "ensure_read_allowed", "ensure_workspace_path"]
```

Create `agent_tools/shared/tool_output.py`:

```python
"""Compatibility facade for normalized tool output helpers."""

from agent_tools.tool_output import tool_error, tool_ok

__all__ = ["tool_error", "tool_ok"]
```

- [ ] **Step 4: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/shared tests/test_agent_tools_public_imports.py
git commit -m "feat: add agent tools shared facade"
```

---

### Task 4: Move Runtime Imports to Public Facades

**Files:**
- Modify: `agent_core/delegation.py`
- Modify: `agent_core/builders.py`
- Modify: `agent_core/system_prompt.py`
- Test: `tests/test_agent_tools_public_imports.py`, `tests/test_terminal_tools.py`, `tests/test_system_prompt.py`

- [ ] **Step 1: Add import-source regression test**

Append to `tests/test_agent_tools_public_imports.py`:

```python
def test_agent_core_uses_public_tool_facades_for_runtime_registration():
    from pathlib import Path

    delegation_source = Path("agent_core/delegation.py").read_text()
    builders_source = Path("agent_core/builders.py").read_text()
    system_prompt_source = Path("agent_core/system_prompt.py").read_text()

    assert "from agent_tools.public.files import" in delegation_source
    assert "from agent_tools.public.terminal import" in delegation_source
    assert "from agent_tools.public.web import" in delegation_source
    assert "from agent_tools.public.skills import" in delegation_source
    assert "from agent_tools.public.memory import" in builders_source
    assert "from agent_tools.public.skills import build_skills_system_prompt" in system_prompt_source
```

- [ ] **Step 2: Run regression test and confirm it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py::test_agent_core_uses_public_tool_facades_for_runtime_registration -v
```

Expected: fails because runtime imports still use top-level compatibility modules.

- [ ] **Step 3: Update `agent_tools/public/skills.py` for prompt helper**

Modify `agent_tools/public/skills.py` to include `build_skills_system_prompt`:

```python
"""LangChain-facing skill discovery and management tools."""

from agent_tools.skill_manage import skill_manage
from agent_tools.skills import build_skills_system_prompt, skill_view, skills_list

__all__ = ["build_skills_system_prompt", "skill_manage", "skill_view", "skills_list"]
```

- [ ] **Step 4: Update `agent_core/delegation.py` imports**

Replace the tool imports in `agent_core/delegation.py` with:

```python
from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_fetch, web_search
```

- [ ] **Step 5: Update `agent_core/builders.py` import**

Replace:

```python
from agent_tools.memory_tools import memory_manage
```

with:

```python
from agent_tools.public.memory import memory_manage
```

- [ ] **Step 6: Update `agent_core/system_prompt.py` import**

Replace:

```python
from agent_tools.skills import build_skills_system_prompt
```

with:

```python
from agent_tools.public.skills import build_skills_system_prompt
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py tests/test_terminal_tools.py tests/test_system_prompt.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add agent_core/delegation.py agent_core/builders.py agent_core/system_prompt.py agent_tools/public/skills.py tests/test_agent_tools_public_imports.py
git commit -m "refactor: use public agent tools facades"
```

---

### Task 5: Move First-Party Implementations Behind Compatibility Shims

**Files:**
- Move: `agent_tools/file_tools.py` -> `agent_tools/public/files.py`
- Move: `agent_tools/terminal_tools.py` -> `agent_tools/public/terminal.py`
- Move: `agent_tools/web.py` -> `agent_tools/public/web.py`
- Move: `agent_tools/memory_tools.py` -> `agent_tools/public/memory.py`
- Move: `agent_tools/skills.py` -> `agent_tools/public/skills.py`
- Move: `agent_tools/skill_manage.py` -> `agent_tools/public/skill_manage_impl.py`
- Modify compatibility shims:
  - `agent_tools/file_tools.py`
  - `agent_tools/terminal_tools.py`
  - `agent_tools/web.py`
  - `agent_tools/memory_tools.py`
  - `agent_tools/skills.py`
  - `agent_tools/skill_manage.py`
- Modify imports inside moved files.
- Test: existing focused tests plus public import tests.

- [ ] **Step 1: Run move with `git mv`**

Run:

```bash
git mv agent_tools/file_tools.py agent_tools/public/files.py
git mv agent_tools/terminal_tools.py agent_tools/public/terminal.py
git mv agent_tools/web.py agent_tools/public/web.py
git mv agent_tools/memory_tools.py agent_tools/public/memory.py
git mv agent_tools/skills.py agent_tools/public/skills.py
git mv agent_tools/skill_manage.py agent_tools/public/skill_manage_impl.py
```

Expected: files are moved under `agent_tools/public/`.

- [ ] **Step 2: Update imports inside moved public files**

In `agent_tools/public/files.py`, replace:

```python
from agent_tools.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path
from agent_tools.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.shared.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path
from agent_tools.shared.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/terminal.py`, replace:

```python
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/web.py`, replace:

```python
from agent_tools.common import truncate
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.shared.common import truncate
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/memory.py`, replace:

```python
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/skills.py`, replace:

```python
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/skill_manage_impl.py`, replace:

```python
from agent_tools.skills import SKILLS_DIR, load_skill_metadata
from agent_tools.tool_output import tool_error, tool_ok
```

with:

```python
from agent_tools.public.skills import SKILLS_DIR, load_skill_metadata
from agent_tools.shared.tool_output import tool_error, tool_ok
```

In `agent_tools/public/skills.py`, import `skill_manage` from the moved implementation:

```python
from agent_tools.public.skill_manage_impl import skill_manage
```

- [ ] **Step 3: Recreate top-level compatibility shims**

Create `agent_tools/file_tools.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.files."""

from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file

__all__ = ["file_info", "list_directory", "patch", "read_file", "search_files", "write_file"]
```

Create `agent_tools/terminal_tools.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.terminal."""

from agent_tools.public.terminal import process, terminal

__all__ = ["process", "terminal"]
```

Create `agent_tools/web.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.web."""

from agent_tools.public.web import web_fetch, web_search

__all__ = ["web_fetch", "web_search"]
```

Create `agent_tools/memory_tools.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.memory."""

from agent_tools.public.memory import memory_manage

__all__ = ["memory_manage"]
```

Create `agent_tools/skills.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.skills."""

from agent_tools.public.skills import (
    SKILLS_DIR,
    build_skills_system_prompt,
    load_skill_metadata,
    skill_view,
    skills_list,
)

__all__ = [
    "SKILLS_DIR",
    "build_skills_system_prompt",
    "load_skill_metadata",
    "skill_view",
    "skills_list",
]
```

Create `agent_tools/skill_manage.py`:

```python
"""Compatibility shim. New code should import from agent_tools.public.skills."""

from agent_tools.public.skill_manage_impl import skill_manage

__all__ = ["skill_manage"]
```

- [ ] **Step 4: Run tests for import compatibility**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py tests/test_terminal_tools.py tests/test_system_prompt.py tests/test_tool_limits.py -v
```

Expected: all tests pass and compatibility imports still return the same tool objects.

- [ ] **Step 5: Run full test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add agent_tools tests/test_agent_tools_public_imports.py
git commit -m "refactor: move first-party tools under public package"
```

---

### Task 6: Move Shared Helper Implementations Behind Compatibility Shims

**Files:**
- Move: `agent_tools/common.py` -> `agent_tools/shared/common.py`
- Move: `agent_tools/file_policy.py` -> `agent_tools/shared/file_policy.py`
- Move: `agent_tools/tool_output.py` -> `agent_tools/shared/tool_output.py`
- Modify compatibility shims:
  - `agent_tools/common.py`
  - `agent_tools/file_policy.py`
  - `agent_tools/tool_output.py`
- Test: shared import tests and full tests.

- [ ] **Step 1: Move shared helper implementations**

Run:

```bash
git mv agent_tools/common.py agent_tools/shared/common.py
git mv agent_tools/file_policy.py agent_tools/shared/file_policy.py
git mv agent_tools/tool_output.py agent_tools/shared/tool_output.py
```

Expected: implementation files now live under `agent_tools/shared/`.

- [ ] **Step 2: Recreate top-level compatibility shims**

Create `agent_tools/common.py`:

```python
"""Compatibility shim. New code should import from agent_tools.shared.common."""

from agent_tools.shared.common import DEFAULT_EXCLUDE_DIRS, path_info, relative_path, truncate

__all__ = ["DEFAULT_EXCLUDE_DIRS", "path_info", "relative_path", "truncate"]
```

Create `agent_tools/file_policy.py`:

```python
"""Compatibility shim. New code should import from agent_tools.shared.file_policy."""

from agent_tools.shared.file_policy import ensure_patch_paths, ensure_read_allowed, ensure_workspace_path

__all__ = ["ensure_patch_paths", "ensure_read_allowed", "ensure_workspace_path"]
```

Create `agent_tools/tool_output.py`:

```python
"""Compatibility shim. New code should import from agent_tools.shared.tool_output."""

from agent_tools.shared.tool_output import tool_error, tool_ok

__all__ = ["tool_error", "tool_ok"]
```

- [ ] **Step 3: Run shared import tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_public_imports.py -v
```

Expected: all shared facade and compatibility tests pass.

- [ ] **Step 4: Run full test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent_tools tests/test_agent_tools_public_imports.py
git commit -m "refactor: move shared agent tool helpers"
```

---

### Task 7: Clean Generated and Duplicate Artifacts

**Files:**
- Delete if present and untracked/ignored:
  - `agent_tools/__pycache__/`
  - `agent_tools/file_toolkit/__pycache__/`
  - `agent_tools/hermes_terminal_toolkit/__pycache__/`
  - `agent_tools/hermes_terminal_toolkit/environments/__pycache__/`
  - `agent_tools/hermes_terminal_toolkit/process_registry copy.py`
- Modify: `.gitignore` if these patterns are not already ignored.

- [ ] **Step 1: Verify duplicate copy is not imported**

Run:

```bash
rg -n "process_registry copy|process_registry\\ copy" agent_core agent_tools tests README.md
```

Expected: no output.

- [ ] **Step 2: Confirm ignore coverage**

Run:

```bash
git check-ignore -v agent_tools/__pycache__/__init__.cpython-311.pyc
git check-ignore -v "agent_tools/hermes_terminal_toolkit/process_registry copy.py"
```

Expected: `__pycache__` is ignored. The copy file may not be ignored.

- [ ] **Step 3: Add targeted ignore for accidental copy files if needed**

If `process_registry copy.py` is not ignored, add to `.gitignore`:

```gitignore
# Accidental local editor/file-manager copies
* copy.py
```

If this pattern is too broad for project policy, use:

```gitignore
agent_tools/hermes_terminal_toolkit/process_registry copy.py
```

- [ ] **Step 4: Remove generated caches and duplicate copy**

Run:

```bash
find agent_tools -type d -name __pycache__ -prune -exec rm -rf {} +
rm -f "agent_tools/hermes_terminal_toolkit/process_registry copy.py"
```

Expected: generated caches and duplicate copy file are gone from the working tree.

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add .gitignore agent_tools
git commit -m "chore: clean agent tools generated artifacts"
```

---

### Task 8: Add Structure Guard Tests

**Files:**
- Create: `tests/test_agent_tools_structure.py`

- [ ] **Step 1: Create structure tests**

Create `tests/test_agent_tools_structure.py`:

```python
from pathlib import Path


def test_agent_tools_has_no_generated_cache_dirs():
    cache_dirs = [path for path in Path("agent_tools").rglob("__pycache__") if path.is_dir()]
    assert cache_dirs == []


def test_agent_tools_has_no_editor_copy_python_files():
    copy_files = [path for path in Path("agent_tools").rglob("* copy.py")]
    assert copy_files == []


def test_agent_core_runtime_imports_preferred_public_facades():
    delegation_source = Path("agent_core/delegation.py").read_text()
    builders_source = Path("agent_core/builders.py").read_text()

    assert "from agent_tools.public." in delegation_source
    assert "from agent_tools.public.memory import memory_manage" in builders_source
```

- [ ] **Step 2: Run structure tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_tools_structure.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Run full tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_tools_structure.py
git commit -m "test: guard agent tools package structure"
```

---

### Task 9: Final Documentation and Verification

**Files:**
- Modify: `README.md`
- Modify: `agent_tools/README.md`

- [ ] **Step 1: Update docs with final import examples**

Ensure `agent_tools/README.md` contains this final example:

```markdown
## Preferred Imports

```python
from agent_tools.public.files import read_file, write_file
from agent_tools.public.terminal import terminal, process
from agent_tools.public.web import web_fetch, web_search
```

Compatibility imports remain supported for older code:

```python
from agent_tools.file_tools import read_file
from agent_tools.terminal_tools import terminal
```
```

- [ ] **Step 2: Compile changed modules**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile \
  agent_tools/public/files.py \
  agent_tools/public/terminal.py \
  agent_tools/public/web.py \
  agent_tools/public/memory.py \
  agent_tools/public/skills.py \
  agent_tools/public/skill_manage_impl.py \
  agent_tools/shared/common.py \
  agent_tools/shared/file_policy.py \
  agent_tools/shared/tool_output.py \
  agent_tools/file_tools.py \
  agent_tools/terminal_tools.py \
  agent_tools/web.py \
  agent_tools/memory_tools.py \
  agent_tools/skills.py \
  agent_tools/skill_manage.py
```

Expected: no output and exit code `0`.

- [ ] **Step 3: Run full test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

Expected: all tests pass.

- [ ] **Step 4: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output and exit code `0`.

- [ ] **Step 5: Run import scan**

Run:

```bash
rg -n "from agent_tools\\.(file_tools|terminal_tools|web|memory_tools|skills|skill_manage|common|file_policy|tool_output) import" agent_core tests
```

Expected: no `agent_core` runtime imports use the top-level compatibility shims. Tests may still import shims only when explicitly testing backward compatibility.

- [ ] **Step 6: Request code review**

Use `superpowers:requesting-code-review` with:

```text
Review range: main..HEAD
Focus: agent_tools package organization, compatibility shims, public/shared facades, import migration, no behavior changes, generated artifact cleanup, and tests.
```

Expected: no Critical or Important findings. Fix any Important findings before merge.

- [ ] **Step 7: Commit final docs if changed**

```bash
git add README.md agent_tools/README.md
git commit -m "docs: document organized agent tools imports"
```

---

## Execution Notes

- Do not move `agent_tools/file_toolkit/` in this plan. It is large, internally cohesive, and already acts as an implementation package.
- Do not move `agent_tools/hermes_terminal_toolkit/` in this plan. Treat it as vendored/imported toolkit code; moving it would require a separate import-path migration and review.
- Keep compatibility shims until all internal and external callers have migrated. Removing them should be a separate deprecation plan.
- Avoid renaming LangChain tool objects. Tool names such as `terminal`, `process`, `read_file`, and `web_search` must remain unchanged.
- Do not change tool schemas or runtime behavior. This plan is organizational only.
- Use `/home/miku/miniforge3/envs/langchain/bin/python` for all Python and pytest commands.

## Self-Review

- Spec coverage: The plan covers every current `agent_tools/` category: public tools, shared helpers, `file_toolkit`, Hermes toolkit, generated caches, duplicate copy file, compatibility exports, runtime imports, tests, and documentation.
- Placeholder scan: No `TBD`, vague implementation instructions, or missing test commands remain.
- Type consistency: The public facade names match existing tool names and the compatibility shims preserve existing import paths.

