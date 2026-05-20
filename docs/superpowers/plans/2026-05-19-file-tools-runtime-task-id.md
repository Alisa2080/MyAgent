# File Tools Runtime Task Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public file tools derive their tracking task id from LangGraph `ToolRuntime`, matching Hermes terminal/process task identity, and remove model-visible `task_id` from file tool schemas.

**Architecture:** Keep low-level `agent_tools.file_toolkit.file_tools` APIs unchanged for now because they already accept `task_id` and are useful for direct tests/internal callers. Change only the public LangChain facade in `agent_tools/public/files.py` so model-facing tools receive `ToolRuntime`, derive `task_id` via `hermes_task_id_from_runtime()`, and pass that internal id into the existing low-level file toolkit. Add regression tests mirroring `tests/test_terminal_tools.py`.

**Tech Stack:** Python, LangChain `@tool`, LangChain `ToolRuntime`, LangGraph `ToolNode`, pytest, existing `agent_core.session_context`.

---

## File Structure

- Modify `agent_tools/public/files.py`
  - Import `ToolRuntime`.
  - Import `hermes_task_id_from_runtime`.
  - Remove `task_id` fields from model-facing input schemas.
  - Add private implementation helpers (`_read_file_impl`, `_write_file_impl`, `_patch_impl`, `_search_files_impl`) that accept `runtime`.
  - Keep low-level calls to `read_file_tool`, `write_file_tool`, `patch_tool`, `search_tool` with an internally derived `task_id`.

- Create `tests/test_file_tools_runtime_task_id.py`
  - Assert file tool schemas do not expose `task_id`.
  - Assert direct helper calls derive task id from `runtime.execution_info.thread_id`.
  - Assert LangGraph `ToolNode` injection derives task id from configurable `thread_id`.
  - Assert fallback runtime `None` still maps to `"default"`.
  - Assert errors do not leak internal `task_id` in public tool metadata.

- Optionally modify `agent_core/system_prompt.py`
  - Only if tests reveal prompt text mentions user-supplied task ids. Current prompt does not, so no prompt change is expected for this specific task.

---

### Task 1: Add Failing Schema Tests

**Files:**
- Create: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Write failing schema tests**

Create `tests/test_file_tools_runtime_task_id.py` with:

```python
def test_file_tool_schemas_do_not_expose_task_id():
    from agent_tools.public.files import patch, read_file, search_files, write_file

    assert "task_id" not in read_file.args
    assert "task_id" not in write_file.args
    assert "task_id" not in patch.args
    assert "task_id" not in search_files.args

    assert "path" in read_file.args
    assert "path" in write_file.args
    assert "mode" in patch.args
    assert "pattern" in search_files.args
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py::test_file_tool_schemas_do_not_expose_task_id -v
```

Expected: FAIL because `read_file`, `write_file`, `patch`, and `search_files` schemas currently expose `task_id`.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_file_tools_runtime_task_id.py
git commit -m "test: cover file tool task id schema"
```

---

### Task 2: Hide `task_id` From Public File Tool Schemas

**Files:**
- Modify: `agent_tools/public/files.py`
- Test: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Remove `task_id` fields from input models**

In `agent_tools/public/files.py`, update imports and schemas:

```python
from langchain.tools import ToolRuntime, tool
```

Remove these fields:

```python
task_id: str = Field(default="default", description="Session/task id for read/write tracking.")
```

from `ReadFileInput`, `WriteFileInput`, `PatchInput`, and `SearchFilesInput`.

- [ ] **Step 2: Run schema test**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py::test_file_tool_schemas_do_not_expose_task_id -v
```

Expected: PASS.

- [ ] **Step 3: Run public import regression test**

Run:

```bash
pytest tests/test_agent_tools_public_imports.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit schema change**

```bash
git add agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
git commit -m "fix: hide file tool task id from schemas"
```

---

### Task 3: Add Runtime-Derived Task Id Tests for Direct Helpers

**Files:**
- Modify: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Add tests for read/write/search/patch helper behavior**

Append these tests:

```python
import json
from types import SimpleNamespace


def test_read_file_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"content": "1|hello\n", "total_lines": 1})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="file-read-thread"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    raw = file_tools._read_file_impl(path="README.md", offset=1, limit=20, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-read-thread")
    assert calls[0]["path"] == "README.md"
    assert "task_id" not in payload.get("meta", {})


def test_write_file_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"bytes_written": 5})

    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-write-thread"))

    raw = file_tools._write_file_impl(path="notes.txt", content="hello", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-write-thread")
    assert calls[0]["content"] == "hello"
    assert "task_id" not in payload.get("meta", {})


def test_search_files_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_search_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"matches": [], "total_count": 0})

    monkeypatch.setattr(file_tools, "search_tool", fake_search_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-search-thread"))

    raw = file_tools._search_files_impl(
        pattern="TODO",
        target="content",
        path=".",
        file_glob=None,
        limit=10,
        offset=0,
        output_mode="content",
        context=0,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-search-thread")
    assert calls[0]["pattern"] == "TODO"
    assert "task_id" not in payload.get("meta", {})


def test_patch_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"message": "Patch applied."})

    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-patch-thread"))

    raw = file_tools._patch_impl(
        mode="replace",
        path="notes.txt",
        old_string="old",
        new_string="new",
        replace_all=False,
        patch=None,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-patch-thread")
    assert calls[0]["old_string"] == "old"
    assert "task_id" not in payload.get("meta", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py -v
```

Expected: FAIL because `_read_file_impl`, `_write_file_impl`, `_search_files_impl`, and `_patch_impl` do not exist yet.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_file_tools_runtime_task_id.py
git commit -m "test: cover runtime scoped file tools"
```

---

### Task 4: Add Runtime-Scoped File Tool Implementations

**Files:**
- Modify: `agent_tools/public/files.py`
- Test: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Import task id resolver**

In `agent_tools/public/files.py`, add:

```python
from agent_core.session_context import hermes_task_id_from_runtime
```

- [ ] **Step 2: Add private implementation helpers**

Insert these helpers after `_wrap_file_tool_result()`:

```python
def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return hermes_task_id_from_runtime(runtime)


def _read_file_impl(path: str, offset: int = 1, limit: int = 500, runtime: ToolRuntime | None = None) -> str:
    path_error = ensure_workspace_path(path)
    if path_error:
        return tool_error("read_file", path_error, code="invalid_path")
    read_error = ensure_read_allowed(path)
    if read_error:
        return tool_error("read_file", read_error, code="access_denied")
    raw = read_file_tool(
        path=path,
        offset=offset,
        limit=limit,
        task_id=_task_id_from_runtime(runtime),
    )
    return _wrap_file_tool_result(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
    )


def _write_file_impl(path: str, content: str, runtime: ToolRuntime | None = None) -> str:
    path_error = ensure_workspace_path(path)
    if path_error:
        return tool_error("write_file", path_error, code="invalid_path")
    raw = write_file_tool(
        path=path,
        content=content,
        task_id=_task_id_from_runtime(runtime),
    )
    return _wrap_file_tool_result("write_file", raw, success_message="File written.")


def _patch_impl(
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    runtime: ToolRuntime | None = None,
) -> str:
    if mode == "replace":
        if not path:
            return tool_error("patch", "path is required for replace mode.", code="invalid_input")
        path_error = ensure_workspace_path(path)
        if path_error:
            return tool_error("patch", path_error, code="invalid_path")
    if mode == "patch":
        path_error = ensure_patch_paths(patch)
        if path_error:
            return tool_error("patch", path_error, code="invalid_path")
    raw = patch_tool(
        mode=mode,
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        patch=patch,
        task_id=_task_id_from_runtime(runtime),
    )
    return _wrap_file_tool_result("patch", raw, success_message="Patch applied.")


def _search_files_impl(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
    runtime: ToolRuntime | None = None,
) -> str:
    path_error = ensure_workspace_path(path)
    if path_error:
        return tool_error("search_files", path_error, code="invalid_path")
    raw = search_tool(
        pattern=pattern,
        target=target,
        path=path,
        file_glob=file_glob,
        limit=limit,
        offset=offset,
        output_mode=output_mode,
        context=context,
        task_id=_task_id_from_runtime(runtime),
    )
    return _wrap_file_tool_result(
        "search_files",
        raw,
        success_message="Search completed.",
        meta_keys=("truncated",),
    )
```

- [ ] **Step 3: Update public tool functions to accept runtime**

Replace `read_file`, `write_file`, `patch`, and `search_files` bodies/signatures with:

```python
@tool("read_file", args_schema=ReadFileInput)
def read_file(path: str, runtime: ToolRuntime, offset: int = 1, limit: int = 500) -> str:
    """Read a text file with line numbers and pagination."""
    return _read_file_impl(path=path, offset=offset, limit=limit, runtime=runtime)
```

```python
@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str, runtime: ToolRuntime) -> str:
    """Write complete content to a workspace file, replacing existing content."""
    return _write_file_impl(path=path, content=content, runtime=runtime)
```

```python
@tool("patch", args_schema=PatchInput)
def patch(
    mode: str = "replace",
    runtime: ToolRuntime = None,
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
) -> str:
    """Apply targeted file edits. Prefer replace mode for small edits."""
    return _patch_impl(
        mode=mode,
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        patch=patch,
        runtime=runtime,
    )
```

```python
@tool("search_files", args_schema=SearchFilesInput)
def search_files(
    pattern: str,
    runtime: ToolRuntime,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
) -> str:
    """Search workspace file contents or find files by name."""
    return _search_files_impl(
        pattern=pattern,
        target=target,
        path=path,
        file_glob=file_glob,
        limit=limit,
        offset=offset,
        output_mode=output_mode,
        context=context,
        runtime=runtime,
    )
```

If type check or runtime rejects `runtime: ToolRuntime = None` for `patch`, use the terminal pattern instead and make it required:

```python
def patch(
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    runtime: ToolRuntime = None,
) -> str:
```

Then verify `patch.args` still excludes `runtime` and `task_id`.

- [ ] **Step 4: Run direct helper tests**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py -v
```

Expected: PASS for schema and direct helper tests.

- [ ] **Step 5: Run terminal regression tests**

Run:

```bash
pytest tests/test_terminal_tools.py tests/test_session_context.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit implementation**

```bash
git add agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
git commit -m "fix: scope file tools by runtime task id"
```

---

### Task 5: Add ToolNode Runtime Injection Tests

**Files:**
- Modify: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Add LangGraph ToolNode tests**

Append:

```python
def test_read_file_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import read_file

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"content": "1|toolnode-ok\n", "total_lines": 1})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([read_file]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "README.md"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-file-read-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-file-read-thread")
    assert "task_id" not in payload.get("meta", {})


def test_write_file_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import write_file

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"bytes_written": 11})

    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([write_file]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "notes.txt", "content": "hello world"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-file-write-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-file-write-thread")
    assert calls[0]["content"] == "hello world"
```

- [ ] **Step 2: Run ToolNode tests**

Run:

```bash
pytest \
  tests/test_file_tools_runtime_task_id.py::test_read_file_toolnode_injects_runtime_thread \
  tests/test_file_tools_runtime_task_id.py::test_write_file_toolnode_injects_runtime_thread \
  -v
```

Expected: PASS.

- [ ] **Step 3: Add patch/search ToolNode tests if runtime injection works for read/write**

Append:

```python
def test_search_files_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import search_files

    calls = []

    def fake_search_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"matches": [], "total_count": 0})

    monkeypatch.setattr(file_tools, "search_tool", fake_search_tool)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([search_files]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_files",
                            "args": {"pattern": "TODO", "target": "content", "path": "."},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-file-search-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-file-search-thread")


def test_patch_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import patch

    calls = []

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"message": "Patch applied."})

    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([patch]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "patch",
                            "args": {
                                "mode": "replace",
                                "path": "notes.txt",
                                "old_string": "old",
                                "new_string": "new",
                            },
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-file-patch-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-file-patch-thread")
```

- [ ] **Step 4: Run all file runtime tests**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit ToolNode tests**

```bash
git add tests/test_file_tools_runtime_task_id.py
git commit -m "test: verify file tool runtime injection through toolnode"
```

---

### Task 6: Preserve Backward-Compatible Internal Fallback

**Files:**
- Modify: `tests/test_file_tools_runtime_task_id.py`
- Modify: `agent_tools/public/files.py` only if the test fails

- [ ] **Step 1: Add fallback test**

Append:

```python
def test_file_tool_impl_falls_back_to_default_task_id_without_runtime(monkeypatch):
    import agent_tools.public.files as file_tools

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"content": "1|fallback\n", "total_lines": 1})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    raw = file_tools._read_file_impl(path="README.md", offset=1, limit=5, runtime=None)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == "default"
```

- [ ] **Step 2: Run fallback test**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py::test_file_tool_impl_falls_back_to_default_task_id_without_runtime -v
```

Expected: PASS because `hermes_task_id_from_runtime(None)` already returns `"default"`.

- [ ] **Step 3: If it fails, fix `_task_id_from_runtime()`**

Use this exact implementation:

```python
def _task_id_from_runtime(runtime: ToolRuntime | None) -> str:
    return hermes_task_id_from_runtime(runtime)
```

- [ ] **Step 4: Run all file runtime tests**

Run:

```bash
pytest tests/test_file_tools_runtime_task_id.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit fallback test**

```bash
git add agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
git commit -m "test: cover default file task id fallback"
```

---

### Task 7: Full Regression and Cleanup

**Files:**
- Inspect: `agent_tools/public/files.py`
- Inspect: `tests/test_file_tools_runtime_task_id.py`
- Inspect: `agent_core/system_prompt.py`

- [ ] **Step 1: Search for remaining public file task id exposure**

Run:

```bash
rg -n "task_id: str|task_id.*Field|Session/task id" agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py agent_core/system_prompt.py
```

Expected: no matches in `agent_tools/public/files.py` schemas or prompt text. Matches inside test assertions or helper expectations are acceptable.

- [ ] **Step 2: Run focused test suite**

Run:

```bash
pytest \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_terminal_tools.py \
  tests/test_session_context.py \
  tests/test_agent_tools_public_imports.py \
  tests/test_system_prompt.py \
  -v
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
pytest -v
```

Expected: PASS.

- [ ] **Step 4: Review diff**

Run:

```bash
git diff -- agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
```

Expected:
- `agent_tools/public/files.py` no longer exposes `task_id` in Pydantic schemas.
- Public tool functions accept `runtime: ToolRuntime`.
- Private helpers derive task id with `hermes_task_id_from_runtime(runtime)`.
- Low-level file toolkit APIs still receive `task_id`.
- No public response metadata includes `task_id`.

- [ ] **Step 5: Commit final cleanup if needed**

If Step 4 required edits:

```bash
git add agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
git commit -m "chore: clean up file runtime task id wiring"
```

If no edits were needed, do not create an empty commit.

---

## Self-Review

**Spec coverage:** The plan covers unified task identity for public file tools, removes model-visible `task_id`, derives the id from `ToolRuntime` using the same `hermes_task_id_from_runtime()` function used by terminal/process, and preserves low-level file toolkit compatibility.

**Out of scope by design:** This plan does not integrate file tools with Hermes active environments, sandbox path mapping, container-aware safety checks, or backend-aware file state. Those are separate follow-up plans because this task is limited to task identity.

**Placeholder scan:** No task contains TBD-style placeholders. The only conditional instruction is for the possible `ToolRuntime` default-position compatibility issue, and it includes exact replacement code and verification.

**Type consistency:** Helper names are `_read_file_impl`, `_write_file_impl`, `_patch_impl`, `_search_files_impl`, and `_task_id_from_runtime`; the same names are used consistently in tests and implementation steps.
