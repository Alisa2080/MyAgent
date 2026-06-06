# Agent Tools Neutralization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Neutralize `agent_tools/` runtime and documentation naming so it reads as project-native LangChain code rather than externally branded reference code.

**Architecture:** Keep public LangChain tool facades stable while renaming only the externally-branded web helper package to `agent_tools.web_toolkit`. Update imports, tests, README, specs, and plans in a controlled sequence, then remove the unused untracked reference file that cannot import in this project.

**Tech Stack:** Python 3.11+, LangChain tool wrappers, pytest, git mv, ripgrep.

---

## Scope Check

This plan covers one cohesive cleanup: neutral naming for `agent_tools` runtime helpers and target documentation. It does not change web provider behavior, public tool schemas, terminal/file toolkit structure, or unrelated dirty working tree items.

## File Structure

Create or rename:

- Rename the previous externally branded web helper package to `agent_tools/web_toolkit/`
  - `__init__.py`: neutral package docstring.
  - `backends.py`: backend selection and provider adapters, unchanged behavior.
  - `content.py`: content cleanup and optional auxiliary summarization, unchanged behavior.
  - `safety.py`: URL safety helpers, unchanged behavior.

Modify:

- `agent_tools/public/web.py`: import from `agent_tools.web_toolkit`.
- `agent_tools/README.md`: describe the neutral web helper package and remove externally branded language.
- `tests/test_web_tools_migration.py`: import from `agent_tools.web_toolkit`.
- `tests/test_agent_tools_public_imports.py`: assert public web surface remains `web_search` / `web_extract`.
- `tests/test_public_toolmessage_results.py`: keep public ToolMessage expectations.
- `docs/superpowers/specs/*.md`: neutralize external project references and rename file names containing external branding.
- `docs/superpowers/plans/*.md`: neutralize external project references and rename file names containing external branding.

Remove:

- `agent_tools/web_tools.py`: untracked reference file that imports non-project modules such as `tools.registry` and `agent.auxiliary_client`.

Do not stage:

- `.codegraph/daemon.pid`
- `agent_cli.backup-before-agent-cli-mvp-20260526/` deletions

## Task 1: Lock Down Neutral Web Helper Imports

**Files:**
- Modify: `tests/test_web_tools_migration.py`
- Modify: `tests/test_agent_tools_public_imports.py`

- [ ] **Step 1: Add failing import assertions for the neutral web helper package**

Append this test to `tests/test_web_tools_migration.py`:

```python
def test_web_toolkit_import_path_is_project_native():
    from agent_tools.web_toolkit import backends, content, safety

    assert hasattr(backends, "get_backend")
    assert hasattr(content, "clean_base64_images")
    assert hasattr(safety, "is_safe_url")
```

- [ ] **Step 2: Update existing helper imports in web migration tests**

In `tests/test_web_tools_migration.py`, replace obsolete web helper imports with:

```python
agent_tools.web_toolkit
```

Also update monkeypatch target strings to:

```python
"agent_tools.web_toolkit.backends.httpx.post"
```

- [ ] **Step 3: Assert no public compatibility surface exposes obsolete web names**

Ensure `tests/test_agent_tools_public_imports.py` keeps these assertions:

```python
def test_web_fetch_removed_from_public_surfaces():
    import agent_tools.public as public
    import agent_tools.public.web as public_web

    assert "web_fetch" not in public.__all__
    assert not hasattr(public_web, "web_fetch")
    assert not hasattr(public, "web_fetch")
```

- [ ] **Step 4: Run tests to verify neutral import path is still missing**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_web_tools_migration.py::test_web_toolkit_import_path_is_project_native \
  tests/test_agent_tools_public_imports.py::test_web_fetch_removed_from_public_surfaces -q
```

Expected: the new import-path test fails with `ModuleNotFoundError: No module named 'agent_tools.web_toolkit'`; the public surface test passes.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_web_tools_migration.py tests/test_agent_tools_public_imports.py
git commit -m "test: require neutral web toolkit imports"
```

## Task 2: Rename Runtime Web Helper Package

**Files:**
- Rename: previous externally branded web helper package -> `agent_tools/web_toolkit/`
- Modify: `agent_tools/public/web.py`
- Modify: `agent_tools/web_toolkit/__init__.py`

- [ ] **Step 1: Rename the package**

Run the corresponding `git mv` from the obsolete helper package name to `agent_tools/web_toolkit`.

Expected: Git records a directory rename containing `__init__.py`, `backends.py`, `content.py`, and `safety.py`.

- [ ] **Step 2: Update public web imports**

In `agent_tools/public/web.py`, import from the neutral helper package:

```python
from agent_tools.web_toolkit.backends import BackendConfigurationError, get_backend
from agent_tools.web_toolkit.content import (
    DEFAULT_MAX_CHARS_PER_URL,
    DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    bound_content,
    clean_base64_images,
    should_summarize,
    summarize_with_auxiliary,
)
from agent_tools.web_toolkit.safety import is_safe_url
```

- [ ] **Step 3: Update package docstring**

Replace the entire contents of `agent_tools/web_toolkit/__init__.py` with:

```python
"""Internal helpers for project-native web tools."""
```

- [ ] **Step 4: Run targeted web tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_web_tools_migration.py \
  tests/test_agent_tools_public_imports.py \
  tests/test_public_toolmessage_results.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit runtime rename**

```bash
git add agent_tools/public/web.py agent_tools/web_toolkit tests/test_web_tools_migration.py tests/test_agent_tools_public_imports.py tests/test_public_toolmessage_results.py
git add -u
git commit -m "refactor: rename web helper toolkit"
```

## Task 3: Remove Unused Reference Web File

**Files:**
- Delete: `agent_tools/web_tools.py`

- [ ] **Step 1: Verify the reference file is untracked and unused**

Run:

```bash
git ls-files agent_tools/web_tools.py
rg -n "agent_tools\\.web_tools|from agent_tools import web_tools|import agent_tools.web_tools" agent_tools agent_core tests cron
```

Expected:

- `git ls-files` prints nothing.
- `rg` prints no runtime imports of `agent_tools.web_tools`.

- [ ] **Step 2: Remove the untracked file**

Run:

```bash
rm -f agent_tools/web_tools.py
```

Expected: `git status --short` no longer lists `?? agent_tools/web_tools.py`.

- [ ] **Step 3: Verify public web tools still import**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python - <<'PY'
from agent_tools.public.web import web_extract, web_search
print(web_search.name, web_extract.name)
PY
```

Expected output:

```text
web_search web_extract
```

- [ ] **Step 4: Commit cleanup only if tracked files changed**

If the file was untracked, there may be nothing to commit. If any tracked test or docs references were updated in this task, commit them:

```bash
git status --short
git diff --name-only
```

Expected: if the only change was removing untracked `agent_tools/web_tools.py`, do not create a commit. If this task also changed tracked docs, run:

```bash
git add docs/superpowers/specs docs/superpowers/plans agent_tools/README.md README.md
git commit -m "chore: remove unused web reference references"
```

## Task 4: Neutralize Agent Tools Documentation

**Files:**
- Modify: `agent_tools/README.md`
- Modify: `docs/superpowers/specs/*.md`
- Modify: `docs/superpowers/plans/*.md`
- Rename: any specs/plans file whose filename contains external branding.

- [ ] **Step 1: Rename web migration docs to neutral filenames**

Rename any specs/plans file whose filename still contains external branding to a neutral filename, including the web migration spec and plan.

Expected: both files are renamed by git.

- [ ] **Step 2: Replace runtime path references**

Run a targeted replacement that updates obsolete web helper package references to `web_toolkit`.

Expected: references to the old web helper package path are replaced with `web_toolkit`.

- [ ] **Step 3: Neutralize external branding words in target docs**

Run replacements or a small script that changes exact external branding words in the target docs to neutral language such as `reference implementation`, `archived reference`, `upstream reference`, `project-native`, `provider-compatible`, and `reference-style`.

Expected: target docs no longer contain the exact external project branding.

- [ ] **Step 4: Manually fix awkward replacements**

Review the remaining changed docs:

```bash
git diff -- docs/superpowers/specs docs/superpowers/plans agent_tools/README.md README.md
```

Fix awkward phrases so they read naturally. Use these checks:

- Avoid self-referential replacement examples after the target terms have already changed.
- Prefer `project-native`, `provider-compatible`, or `reference implementation` based on sentence meaning.
- Keep historical reference paths only when they still clarify where an old design came from.
- Ensure rewritten sentences do not contain doubled spaces, broken possessives, or temporary placeholder identifiers.

Expected: docs preserve technical meaning and read naturally.

- [ ] **Step 5: Verify no target branding remains**

Run:

```bash
rg -n "[H]ermes|[h]ermes|web_[h]ermes" agent_tools tests docs/superpowers/specs docs/superpowers/plans README.md agent_tools/README.md
```

Expected: no matches in target paths.

- [ ] **Step 6: Commit documentation neutralization**

```bash
git add docs/superpowers/specs docs/superpowers/plans agent_tools/README.md README.md tests agent_tools agent_core
git commit -m "docs: neutralize agent tools reference naming"
```

## Task 5: Final Verification and Review

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run targeted web tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_web_tools_migration.py \
  tests/test_agent_tools_public_imports.py \
  tests/test_public_toolmessage_results.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/ -q
```

Expected: full suite passes.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check HEAD
```

Expected: no output and exit code 0.

- [ ] **Step 4: Confirm target naming cleanup**

Run:

```bash
rg -n "[H]ermes|[h]ermes|web_[h]ermes" agent_tools tests docs/superpowers/specs docs/superpowers/plans README.md agent_tools/README.md
```

Expected: no matches.

- [ ] **Step 5: Confirm unrelated dirty items were not staged**

Run:

```bash
git status --short
git diff --cached --name-status
```

Expected:

- `.codegraph/daemon.pid` may remain unstaged.
- `agent_cli.backup-before-agent-cli-mvp-20260526/` deletions may remain unstaged unless the user explicitly asks to handle them.
- No deleted backup files or runtime PID files are staged.

- [ ] **Step 6: Request code review**

Use `superpowers:requesting-code-review` for the final diff range.

Reviewer context:

- Runtime rename from externally-branded web helper package to `web_toolkit`.
- Public tools remain `web_search` / `web_extract`.
- `web_fetch` remains removed.
- Docs and historical plans/specs neutralized.
- Untracked unusable reference file removed from workspace.

- [ ] **Step 7: Address review feedback**

If the reviewer finds issues, use `superpowers:receiving-code-review`, verify each issue, and fix with tests first where behavior changes are involved.

- [ ] **Step 8: Commit final review fixes if needed**

```bash
git status --short
git add agent_tools tests docs/superpowers/specs docs/superpowers/plans README.md
git commit -m "fix: address agent tools neutralization review"
```

Expected: only create this commit if review fixes were necessary. Before committing, verify `git diff --cached --name-status` does not include `.codegraph/daemon.pid` or `agent_cli.backup-before-agent-cli-mvp-20260526/`.

## Self-Review

Spec coverage:

- Runtime package rename is covered by Tasks 1 and 2.
- Untracked reference file cleanup is covered by Task 3.
- README/spec/plan neutralization is covered by Task 4.
- Verification and code review are covered by Task 5.
- Out-of-scope dirty items are explicitly excluded.

Placeholder scan:

- No placeholder instructions remain.
- Commands and expected outcomes are explicit.

Type consistency:

- New package path is consistently `agent_tools.web_toolkit`.
- Public tool names remain `web_search` and `web_extract`.
- Obsolete `web_fetch` remains absent from public surfaces.
