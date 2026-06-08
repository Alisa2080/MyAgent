# Remove PolicyToolMiddleware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the obsolete `PolicyToolMiddleware` entry point so execution-time policy enforcement only runs through the ToolBus policy pre-hook.

**Architecture:** `agent_core.policy_tool_gate` remains the single policy module for canonical arg builders, `run_policy_tool_gate(...)`, and `build_policy_pre_hook(...)`. Runtime imports move from `agent_core.policy_tool_middleware` to `agent_core.policy_tool_gate`; middleware-specific tests are deleted after equivalent behavior is covered through ToolBus pre-hook integration tests.

**Tech Stack:** Python 3.11, LangChain `ToolMessage`, existing `ToolBusMiddleware`, existing `policy_tool_gate`, pytest, ripgrep, project conda interpreter `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## File Structure

- Delete `agent_core/policy_tool_middleware.py`
  - Remove the old `AgentMiddleware` wrapper entirely. Do not leave a shim module or deprecated class.
- Modify `agent_tools/public/terminal.py`
  - Import `process_policy_args` and `terminal_policy_args` from `agent_core.policy_tool_gate`.
- Modify `agent_tools/public/files.py`
  - Import `patch_policy_args` and `write_file_policy_args` from `agent_core.policy_tool_gate`.
- Delete `tests/test_policy_tool_middleware.py`
  - Former middleware behavior is covered by `tests/test_policy_tool_gate.py`, `tests/test_tool_bus_middleware.py`, and `tests/test_policy_tool_bus_integration.py`.
- Modify `tests/test_policy_tool_bus_integration.py`
  - Replace the nested `ToolBusMiddleware + PolicyToolMiddleware` helper with real `ToolBusMiddleware + build_policy_pre_hook`.
  - Assert blocked policy results run post hooks but skip transform hooks and result limiting.
- Modify `tests/test_permissions_terminal_wrappers.py`
  - Import `terminal_policy_args` from `agent_core.policy_tool_gate`.
- Modify `tests/test_permissions_process_wrappers.py`
  - Import `process_policy_args` from `agent_core.policy_tool_gate`.
- Modify `tests/test_file_tools_terminal_env.py`
  - Import `write_file_policy_args` and `patch_policy_args` from `agent_core.policy_tool_gate`.
- Modify `tests/test_agent_cli_builders.py`
  - Remove the obsolete fake `PolicyToolMiddleware` monkeypatch.
  - Keep the assertion that the production `ToolBusMiddleware` receives the sentinel policy pre-hook.
- Modify `docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md`
  - Update the current effective design so it says the compatibility wrapper was removed by the follow-up deletion design.

## Task 1: Rewrite Policy ToolBus Integration Tests

**Files:**
- Modify: `tests/test_policy_tool_bus_integration.py`
- Read: `agent_core/policy_tool_gate.py`
- Read: `agent_core/tool_bus_middleware.py`

- [ ] **Step 1: Replace the integration test file with ToolBus policy pre-hook coverage**

Replace the full contents of `tests/test_policy_tool_bus_integration.py` with:

```python
from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_core.permissions.approvals import clear_approvals
from agent_core.permissions.tool_grants import clear_tool_policy_grants
from agent_core.session_context import runtime_task_id_from_thread_id


def setup_function():
    clear_approvals()
    clear_tool_policy_grants()


def teardown_function():
    clear_approvals()
    clear_tool_policy_grants()


def _runtime(thread_id: str = "policy-toolbus-thread", tool_call_id: str = "call-policy-toolbus"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _request(
    tool_name: str,
    args: dict,
    *,
    thread_id: str = "policy-toolbus-thread",
    tool_call_id: str = "call-policy-toolbus",
):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id},
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
        tool=SimpleNamespace(name=tool_name, args={}),
        state={},
    )


def _success_message(tool_name: str, tool_call_id: str) -> ToolMessage:
    return ToolMessage(
        content="ok",
        name=tool_name,
        tool_call_id=tool_call_id,
        status="success",
        artifact={
            "ok": True,
            "tool": tool_name,
            "message": "ok",
            "data": None,
            "error": None,
            "meta": {},
        },
    )


def _run_policy_toolbus(
    request,
    handler,
    *,
    post_calls: list | None = None,
    transform_calls: list | None = None,
    max_result_size_chars: int | None = None,
):
    from agent_core.policy_tool_gate import build_policy_pre_hook
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    hooks = ToolBusHooks(
        pre_tool_call=[
            build_policy_pre_hook(
                policy_tools={"terminal", "process", "write_file", "patch"},
            )
        ]
    )
    if post_calls is not None:
        hooks.post_tool_call.append(
            lambda bus_request, bus_result: post_calls.append(
                (
                    bus_request.tool_name,
                    bus_result.result.status,
                    bus_result.result.artifact["error"]["code"]
                    if bus_result.result.artifact.get("error")
                    else None,
                )
            )
        )
    if transform_calls is not None:
        hooks.transform_tool_result.append(
            lambda bus_request, result: transform_calls.append(result)
            or _success_message(bus_request.tool_name, bus_request.tool_call_id)
        )

    specs = None
    if max_result_size_chars is not None:
        specs = {
            request.tool_call["name"]: ToolSpec(
                name=request.tool_call["name"],
                toolset="test",
                tool=request.tool,
                max_result_size_chars=max_result_size_chars,
            )
        }

    bus = ToolBusMiddleware(hooks=hooks, specs=specs)
    return bus.wrap_tool_call(request, handler)


def test_toolbus_policy_pre_hook_allows_handler_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args

    request = _request("terminal", {"command": "pwd"}, tool_call_id="call-allow")
    calls = []
    post_calls = []

    result = _run_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-allow"),
        post_calls=post_calls,
    )

    assert result.artifact["ok"] is True
    assert calls == [request]
    assert post_calls == [("terminal", "success", None)]

    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-toolbus-thread"),
        tool_call_id="call-allow",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ()


def test_toolbus_policy_pre_hook_deny_blocks_handler_and_posts_result():
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")
    calls = []
    post_calls = []
    transform_calls = []

    result = _run_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-deny"),
        post_calls=post_calls,
        transform_calls=transform_calls,
        max_result_size_chars=10,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert post_calls == [("terminal", "error", "policy_denied")]
    assert transform_calls == []
    assert "truncated" not in result.content


def test_toolbus_policy_pre_hook_review_without_approval_blocks_handler():
    request = _request(
        "terminal",
        {"command": "touch approval-required.txt"},
        tool_call_id="call-review-missing",
    )
    calls = []
    post_calls = []
    transform_calls = []

    result = _run_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-review-missing"),
        post_calls=post_calls,
        transform_calls=transform_calls,
        max_result_size_chars=10,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "approval_required"
    assert post_calls == [("terminal", "error", "approval_required")]
    assert transform_calls == []
    assert "truncated" not in result.content


def test_toolbus_policy_pre_hook_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, consume_approval, make_args_digest, record_approval
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args

    thread_id = "policy-toolbus-network-thread"
    tool_call_id = "call-network"
    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=runtime_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )
    request = _request(
        "terminal",
        {"command": "curl https://example.com"},
        thread_id=thread_id,
        tool_call_id=tool_call_id,
    )
    calls = []

    result = _run_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", tool_call_id),
    )

    assert result.artifact["ok"] is True
    assert calls == [request]
    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args=args,
            required_risk_tags=("network_access",),
        )
        is None
    )

    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id(thread_id),
        tool_call_id=tool_call_id,
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ("network_access",)
    assert grant.allow_network_once is True
```

- [ ] **Step 2: Run integration tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_bus_integration.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit integration rewrite**

```bash
git add tests/test_policy_tool_bus_integration.py
git commit -m "test: cover policy through toolbus pre-hook"
```

## Task 2: Move Runtime and Wrapper Test Imports to Policy Gate

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Modify: `agent_tools/public/files.py`
- Modify: `tests/test_permissions_terminal_wrappers.py`
- Modify: `tests/test_permissions_process_wrappers.py`
- Modify: `tests/test_file_tools_terminal_env.py`

- [ ] **Step 1: Update runtime imports**

In `agent_tools/public/terminal.py`, replace:

```python
from agent_core.policy_tool_middleware import process_policy_args, terminal_policy_args
```

with:

```python
from agent_core.policy_tool_gate import process_policy_args, terminal_policy_args
```

In `agent_tools/public/files.py`, replace:

```python
from agent_core.policy_tool_middleware import patch_policy_args, write_file_policy_args
```

with:

```python
from agent_core.policy_tool_gate import patch_policy_args, write_file_policy_args
```

- [ ] **Step 2: Update wrapper test imports**

In `tests/test_permissions_terminal_wrappers.py`, replace:

```python
from agent_core.policy_tool_middleware import terminal_policy_args
```

with:

```python
from agent_core.policy_tool_gate import terminal_policy_args
```

In `tests/test_permissions_process_wrappers.py`, replace:

```python
from agent_core.policy_tool_middleware import process_policy_args
```

with:

```python
from agent_core.policy_tool_gate import process_policy_args
```

In `tests/test_file_tools_terminal_env.py`, replace each old import:

```python
from agent_core.policy_tool_middleware import write_file_policy_args
from agent_core.policy_tool_middleware import patch_policy_args
```

with:

```python
from agent_core.policy_tool_gate import write_file_policy_args
from agent_core.policy_tool_gate import patch_policy_args
```

- [ ] **Step 3: Run wrapper tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_terminal_wrappers.py \
  tests/test_permissions_process_wrappers.py \
  tests/test_permissions_file_wrappers.py \
  tests/test_file_tools_terminal_env.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Commit import migration**

```bash
git add \
  agent_tools/public/terminal.py \
  agent_tools/public/files.py \
  tests/test_permissions_terminal_wrappers.py \
  tests/test_permissions_process_wrappers.py \
  tests/test_file_tools_terminal_env.py
git commit -m "refactor: import policy args from policy gate"
```

## Task 3: Delete PolicyToolMiddleware and Middleware-Specific Tests

**Files:**
- Delete: `agent_core/policy_tool_middleware.py`
- Delete: `tests/test_policy_tool_middleware.py`
- Modify: `tests/test_agent_cli_builders.py`

- [ ] **Step 1: Delete obsolete files**

Delete these files:

```text
agent_core/policy_tool_middleware.py
tests/test_policy_tool_middleware.py
```

- [ ] **Step 2: Remove obsolete builder test monkeypatch**

In `tests/test_agent_cli_builders.py`, update `test_build_agent_wires_policy_pre_hook_into_toolbus`.

Keep this class:

```python
    class FakeToolBus:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
```

Remove this class:

```python
    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
```

Remove this monkeypatch:

```python
    monkeypatch.setattr(builders, "PolicyToolMiddleware", FakePolicy, raising=False)
```

Remove these assertions:

```python
    policy_items = [item for item in middleware if isinstance(item, FakePolicy)]
    assert policy_items == []
```

Add this assertion after `middleware = agent_config["middleware"]`:

```python
    assert not any(type(item).__name__ == "PolicyToolMiddleware" for item in middleware)
```

- [ ] **Step 3: Run core policy and builder tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_policy_tool_gate.py \
  tests/test_policy_tool_bus_integration.py \
  tests/test_tool_bus_middleware.py \
  tests/test_agent_cli_builders.py \
  tests/test_permissions_human_loop.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Confirm old runtime and test references are gone**

Run:

```bash
rg -n "PolicyToolMiddleware|agent_core\\.policy_tool_middleware|policy_tool_middleware" \
  agent_core agent_tools tests
```

Expected: no output and exit code 1.

- [ ] **Step 5: Commit deletion**

```bash
git add agent_core/policy_tool_middleware.py tests/test_policy_tool_middleware.py tests/test_agent_cli_builders.py
git commit -m "refactor: remove policy tool middleware"
```

## Task 4: Update Current Effective Design Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md`
- Read: `docs/superpowers/specs/2026-06-09-remove-policy-tool-middleware-design.md`

- [ ] **Step 1: Update the current design decision**

In `docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md`, replace the compatibility-wrapper decision with this text:

```markdown
The migration originally kept `PolicyToolMiddleware` as a temporary
compatibility wrapper. The follow-up deletion design
`2026-06-09-remove-policy-tool-middleware-design.md` removes that wrapper, so
`agent_core.policy_tool_gate` is the only execution-time policy gate module and
production enforcement runs only through the ToolBus policy pre-hook.
```

- [ ] **Step 2: Update verification and success criteria**

In the same file, remove `tests/test_policy_tool_middleware.py` from current target test lists.

Replace this success criterion:

```markdown
- `PolicyToolMiddleware` remains as a compatibility wrapper around the shared
  gate.
```

with:

```markdown
- The old `PolicyToolMiddleware` module has been removed; policy behavior is
  covered by gate, ToolBus, integration, and builder tests.
```

- [ ] **Step 3: Run current documentation reference check**

Run:

```bash
rg -n "compatibility wrapper|tests/test_policy_tool_middleware.py|agent_core\\.policy_tool_middleware" \
  docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md
```

Expected: no output and exit code 1.

- [ ] **Step 4: Commit documentation update**

```bash
git add docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md
git commit -m "docs: reflect policy middleware removal"
```

## Task 5: Final Verification

**Files:**
- Verify: all files changed in Tasks 1-4

- [ ] **Step 1: Run required verification suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_policy_tool_gate.py \
  tests/test_tool_bus_middleware.py \
  tests/test_policy_tool_bus_integration.py \
  tests/test_agent_cli_builders.py \
  tests/test_permissions_human_loop.py \
  tests/test_permissions_terminal_wrappers.py \
  tests/test_permissions_process_wrappers.py \
  tests/test_permissions_file_wrappers.py \
  tests/test_file_tools_terminal_env.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run old runtime and test reference search**

Run:

```bash
rg -n "PolicyToolMiddleware|agent_core\\.policy_tool_middleware|policy_tool_middleware" \
  agent_core agent_tools tests
```

Expected: no output and exit code 1.

- [ ] **Step 3: Run current effective design reference search**

Run:

```bash
rg -n "compatibility wrapper|tests/test_policy_tool_middleware.py|agent_core\\.policy_tool_middleware" \
  docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md
```

Expected: no output and exit code 1.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 5: Inspect final status**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: clean worktree and recent commits from this plan visible.
