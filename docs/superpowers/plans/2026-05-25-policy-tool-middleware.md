# Policy Tool Middleware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize execution-time policy evaluation and approval consumption for `terminal`, `process`, `write_file`, and `patch` in a LangChain `wrap_tool_call` middleware.

**Architecture:** Add a focused `PolicyToolMiddleware` that evaluates supported tool calls, consumes review approvals, records a short-lived per-call grant, and short-circuits denied calls with standard tool errors. Keep terminal/process/file wrappers responsible for business execution, with a compatibility fallback when wrappers are called outside middleware. Wire the middleware into agent builders alongside the existing human-loop middleware.

**Tech Stack:** Python 3.11, LangChain `AgentMiddleware.wrap_tool_call`, LangGraph `ToolCallRequest`, existing `RuntimeContext`, existing `tool_policy`, `file_policy`, and approval store.

---

## File Structure

- Create `agent_core/permissions/tool_grants.py`
  - Owns `ToolPolicyGrant`, `record_tool_policy_grant`, `consume_tool_policy_grant`, and `clear_tool_policy_grants`.
  - This is intentionally separate from persisted human approvals because grants are execution-local proof that middleware already consumed an approval.

- Create `agent_core/policy_tool_middleware.py`
  - Owns `PolicyToolMiddleware`.
  - Owns canonical policy arg builders for `terminal`, `process`, `write_file`, and `patch`.
  - Owns deny/review short-circuit error conversion.

- Modify `agent_tools/public/terminal.py`
  - Import policy arg builders from `agent_core.policy_tool_middleware`.
  - Skip primary `tool_policy.evaluate_tool_call` and `consume_approval` paths from `_terminal_impl` and `_process_impl` when a middleware grant exists.
  - Read grants from `tool_grants`; use fallback helper when middleware is bypassed.

- Modify `agent_tools/public/files.py`
  - Import write/patch policy arg builders from `agent_core.policy_tool_middleware`.
  - Change file approval helpers to prefer middleware grants and only fallback to direct approval consumption when no grant exists.
  - Keep path classification and approved root calculation inside this file.

- Modify `agent_core/builders.py`
  - Import and register `PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS)` after `FlexibleHumanInTheLoopMiddleware`.

- Add `tests/test_policy_tool_middleware.py`
  - Unit tests for middleware allow, deny, review consumed, missing approval, grant creation, unsupported pass-through, and builder wiring.

- Update existing tests:
  - `tests/test_permissions_terminal_wrappers.py`
  - `tests/test_permissions_process_wrappers.py`
  - `tests/test_file_tools_hermes_env.py`

---

## Task 1: Add Per-Call Tool Policy Grants

**Files:**
- Create: `agent_core/permissions/tool_grants.py`
- Test: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Write failing grant store tests**

Add the initial test file:

```python
from agent_core.permissions.tool_grants import (
    ToolPolicyGrant,
    clear_tool_policy_grants,
    consume_tool_policy_grant,
    record_tool_policy_grant,
)


def setup_function():
    clear_tool_policy_grants()


def teardown_function():
    clear_tool_policy_grants()


def test_tool_policy_grant_is_consumed_once():
    grant = ToolPolicyGrant(
        task_id="task-grant",
        tool_call_id="call-grant",
        tool_name="terminal",
        risk_tags=("network_access",),
        allow_network_once=True,
    )

    record_tool_policy_grant(grant)

    consumed = consume_tool_policy_grant(
        task_id="task-grant",
        tool_call_id="call-grant",
        tool_name="terminal",
    )
    assert consumed == grant
    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="terminal",
        )
        is None
    )


def test_tool_policy_grant_requires_matching_tool_name():
    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="terminal",
            risk_tags=("destructive_command",),
        )
    )

    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="process",
        )
        is None
    )


def test_tool_policy_grant_requires_matching_risk_tags():
    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
            risk_tags=(),
        )
    )

    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
            required_risk_tags=("external_file_write",),
        )
        is None
    )
    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
        )
        is not None
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.permissions.tool_grants'`.

- [ ] **Step 3: Implement grant store**

Create `agent_core/permissions/tool_grants.py`:

```python
from __future__ import annotations

import threading
from dataclasses import dataclass

from agent_core.permissions.models import RiskTag


@dataclass(frozen=True)
class ToolPolicyGrant:
    task_id: str
    tool_call_id: str
    tool_name: str
    risk_tags: tuple[RiskTag, ...]
    allow_network_once: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_tags", tuple(self.risk_tags))


_lock = threading.Lock()
_grants: dict[tuple[str, str], ToolPolicyGrant] = {}


def record_tool_policy_grant(grant: ToolPolicyGrant) -> None:
    with _lock:
        _grants[(grant.task_id, grant.tool_call_id)] = grant


def consume_tool_policy_grant(
    *,
    task_id: str,
    tool_call_id: str | None,
    tool_name: str,
    required_risk_tags: tuple[RiskTag, ...] = (),
) -> ToolPolicyGrant | None:
    if not tool_call_id:
        return None
    key = (task_id, tool_call_id)
    with _lock:
        grant = _grants.get(key)
        if grant is None:
            return None
        if grant.tool_name != tool_name:
            return None
        if not set(required_risk_tags).issubset(set(grant.risk_tags)):
            return None
        return _grants.pop(key)


def clear_tool_policy_grants() -> None:
    with _lock:
        _grants.clear()
```

- [ ] **Step 4: Run grant tests**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/permissions/tool_grants.py tests/test_policy_tool_middleware.py
git commit -m "feat: add tool policy grant store"
```

---

## Task 2: Add PolicyToolMiddleware

**Files:**
- Create: `agent_core/policy_tool_middleware.py`
- Modify: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Add failing middleware tests**

Append these tests to `tests/test_policy_tool_middleware.py`:

```python
import json
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_core.permissions.approvals import (
    ApprovalRecord,
    clear_approvals,
    make_args_digest,
    record_approval,
)
from agent_core.permissions.tool_grants import consume_tool_policy_grant
from agent_core.permissions.tool_policy import canonical_tool_args
from agent_core.policy_tool_middleware import PolicyToolMiddleware
from agent_core.session_context import hermes_task_id_from_thread_id


def _runtime(thread_id="policy-thread", tool_call_id="call-policy"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _request(tool_name, args, *, thread_id="policy-thread", tool_call_id="call-policy"):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id},
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
        tool=None,
        state={},
    )


def test_policy_tool_middleware_passes_allow_to_handler():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("terminal", {"command": "pwd"})
    calls = []

    def handler(received):
        calls.append(received)
        return ToolMessage(content='{"ok": true}', name="terminal", tool_call_id="call-policy")

    result = middleware.wrap_tool_call(request, handler)

    assert result.content == '{"ok": true}'
    assert calls == [request]
    grant = consume_tool_policy_grant(
        task_id=hermes_task_id_from_thread_id("policy-thread"),
        tool_call_id="call-policy",
        tool_name="terminal",
    )
    assert grant is not None
    assert grant.risk_tags == ()


def test_policy_tool_middleware_short_circuits_deny():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    calls = []
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")

    result = middleware.wrap_tool_call(
        request,
        lambda received: calls.append(received),
    )

    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert result.status == "error"
    assert calls == []


def test_policy_tool_middleware_requires_approval_for_review():
    clear_approvals()
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("terminal", {"command": "touch approval-required.txt"}, tool_call_id="call-review")

    result = middleware.wrap_tool_call(request, lambda received: ToolMessage(content="unused", tool_call_id="x"))

    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"


def test_policy_tool_middleware_consumes_approval_and_records_grant():
    clear_approvals()
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request(
        "terminal",
        {"command": "curl https://example.com"},
        thread_id="thread-network",
        tool_call_id="call-network",
    )
    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=hermes_task_id_from_thread_id("thread-network"),
            tool_call_id="call-network",
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(
            content='{"ok": true}',
            name="terminal",
            tool_call_id="call-network",
        ),
    )

    assert json.loads(result.content)["ok"] is True
    grant = consume_tool_policy_grant(
        task_id=hermes_task_id_from_thread_id("thread-network"),
        tool_call_id="call-network",
        tool_name="terminal",
    )
    assert grant is not None
    assert grant.allow_network_once is True
    assert grant.risk_tags == ("network_access",)


def test_policy_tool_middleware_ignores_unsupported_tool():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("read_file", {"path": "README.md"}, tool_call_id="call-read")

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(
            content='{"ok": true}',
            name="read_file",
            tool_call_id="call-read",
        ),
    )

    assert json.loads(result.content)["ok"] is True
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.policy_tool_middleware'`.

- [ ] **Step 3: Implement middleware and policy arg builders**

Create `agent_core/policy_tool_middleware.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval
from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
from agent_core.session_context import RuntimeContext
from agent_tools.shared.tool_output import tool_error


def terminal_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args(
        "terminal",
        {
            "command": args.get("command", ""),
            "background": args.get("background", False),
            "timeout": args.get("timeout"),
            "workdir": args.get("workdir"),
            "pty": args.get("pty", False),
            "notify_on_complete": args.get("notify_on_complete", False),
            "watch_patterns": args.get("watch_patterns"),
        },
    )


def process_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args(
        "process",
        {
            "action": args.get("action", ""),
            "session_id": args.get("session_id", ""),
            "data": args.get("data", ""),
            "timeout": args.get("timeout"),
            "offset": args.get("offset", 0),
            "limit": args.get("limit", 200),
        },
    )


def write_file_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args(
        "write_file",
        {
            "path": args.get("path", ""),
            "content": args.get("content", ""),
        },
    )


def patch_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args(
        "patch",
        {
            "mode": args.get("mode", "replace"),
            "path": args.get("path"),
            "old_string": args.get("old_string"),
            "new_string": args.get("new_string"),
            "replace_all": args.get("replace_all", False),
            "patch": args.get("patch"),
        },
    )


POLICY_ARG_BUILDERS = {
    "terminal": terminal_policy_args,
    "process": process_policy_args,
    "write_file": write_file_policy_args,
    "patch": patch_policy_args,
}


class PolicyToolMiddleware(AgentMiddleware):
    def __init__(self, *, policy_tools: set[str]) -> None:
        super().__init__()
        self.policy_tools = set(policy_tools)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tool_call = request.tool_call
        tool_name = tool_call["name"]
        if tool_name not in self.policy_tools:
            return handler(request)

        builder = POLICY_ARG_BUILDERS.get(tool_name)
        if builder is None:
            return handler(request)

        runtime_context = RuntimeContext.from_runtime(request.runtime)
        tool_call_id = tool_call.get("id") or runtime_context.tool_call_id
        policy_args = builder(tool_call.get("args") or {})
        decision = tool_policy.evaluate_tool_call(
            tool_name,
            policy_args,
            runtime_context.task_id,
            tool_call_id=tool_call_id,
        )
        if decision.outcome == "deny":
            return self._tool_message(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content=tool_error(
                    tool_name,
                    decision.human_message,
                    code="policy_denied",
                    data=decision.data,
                ),
                status="error",
            )
        if decision.outcome == "allow":
            if tool_call_id:
                record_tool_policy_grant(
                    ToolPolicyGrant(
                        task_id=runtime_context.task_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        risk_tags=decision.risk_tags,
                    )
                )
        if decision.outcome == "review":
            approval = consume_approval(
                task_id=runtime_context.task_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=policy_args,
                required_risk_tags=decision.risk_tags,
            )
            if approval is None:
                return self._tool_message(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    content=tool_error(
                        tool_name,
                        decision.human_message,
                        code="approval_required",
                        data=decision.data,
                    ),
                    status="error",
                )
            record_tool_policy_grant(
                ToolPolicyGrant(
                    task_id=runtime_context.task_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    risk_tags=approval.risk_tags,
                    allow_network_once=approval.allow_network_once,
                )
            )
        return handler(request)

    @staticmethod
    def _tool_message(
        *,
        tool_name: str,
        tool_call_id: str | None,
        content: str,
        status: str,
    ) -> ToolMessage:
        return ToolMessage(
            content=content,
            name=tool_name,
            tool_call_id=tool_call_id or "",
            status=status,
        )
```

- [ ] **Step 4: Run middleware tests**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/policy_tool_middleware.py tests/test_policy_tool_middleware.py
git commit -m "feat: add policy tool middleware"
```

---

## Task 3: Migrate Terminal and Process Wrappers to Grants

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Modify: `tests/test_permissions_terminal_wrappers.py`
- Modify: `tests/test_permissions_process_wrappers.py`

- [ ] **Step 1: Add failing wrapper grant-path tests**

In `tests/test_permissions_terminal_wrappers.py`, add:

```python
def test_terminal_uses_middleware_grant_without_consuming_approval(monkeypatch):
    from agent_core.permissions.approvals import clear_approvals
    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    clear_approvals()
    calls = []
    thread_id = "terminal-grant-thread"
    tool_call_id = "call-terminal-grant"

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0, "error": None}),
    )

    raw = terminal_tools._terminal_impl(
        command="curl https://example.com",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    assert json.loads(raw)["ok"] is True
    assert calls[0]["force"] is True
    assert calls[0]["allow_network_once"] is True
```

In `tests/test_permissions_process_wrappers.py`, add:

```python
def test_process_uses_middleware_grant_without_consuming_approval(monkeypatch):
    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.session_context import hermes_task_id_from_thread_id

    terminal_tools = _terminal_module()
    calls = []
    thread_id = "process-grant-thread"
    tool_call_id = "call-process-grant"

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="process",
            risk_tags=("process_stdin",),
        )
    )

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: _owning_session(thread_id))
    monkeypatch.setattr(
        terminal_tools,
        "run_process",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"session_id": "proc_1", "submitted": True}),
    )

    raw = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
    )

    assert json.loads(raw)["ok"] is True
    assert calls[0]["data"] == "exit"
```

- [ ] **Step 2: Run wrapper tests to verify failure**

Run:

```bash
pytest tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py -q
```

Expected: FAIL because `_terminal_impl` and `_process_impl` still require direct approval consumption.

- [ ] **Step 3: Refactor terminal wrapper**

In `agent_tools/public/terminal.py`:

1. Replace imports:

```python
from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval
```

with:

```python
from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval
from agent_core.permissions.tool_grants import consume_tool_policy_grant
from agent_core.policy_tool_middleware import process_policy_args, terminal_policy_args
```

2. Keep `_terminal_policy_args` and `_process_policy_args` as compatibility wrappers:

```python
def _terminal_policy_args(
    *,
    command: str,
    background: bool,
    timeout: int | None,
    workdir: str | None,
    pty: bool,
    notify_on_complete: bool,
    watch_patterns: list[str] | None,
) -> dict:
    return terminal_policy_args(
        {
            "command": command,
            "background": background,
            "timeout": timeout,
            "workdir": workdir,
            "pty": pty,
            "notify_on_complete": notify_on_complete,
            "watch_patterns": watch_patterns,
        }
    )
```

and:

```python
def _process_policy_args(
    *,
    action: str,
    session_id: str,
    data: str,
    timeout: int | None,
    offset: int,
    limit: int,
) -> dict:
    return process_policy_args(
        {
            "action": action,
            "session_id": session_id,
            "data": data,
            "timeout": timeout,
            "offset": offset,
            "limit": limit,
        }
    )
```

3. Add a fallback helper:

```python
def _approval_or_grant_for_review(
    *,
    tool_name: str,
    policy_args: dict,
    decision,
    runtime_context: RuntimeContext,
):
    grant = consume_tool_policy_grant(
        task_id=runtime_context.task_id,
        tool_call_id=runtime_context.tool_call_id,
        tool_name=tool_name,
        required_risk_tags=decision.risk_tags,
    )
    if grant is not None:
        return grant
    return consume_approval(
        task_id=runtime_context.task_id,
        tool_call_id=runtime_context.tool_call_id,
        tool_name=tool_name,
        args=policy_args,
        required_risk_tags=decision.risk_tags,
    )
```

4. In `_terminal_impl`, consume a grant before policy evaluation. If a grant exists, set `force=True` when `grant.risk_tags` is non-empty, set `allow_network_once=grant.allow_network_once`, and skip evaluation. If no grant exists, keep evaluation for direct-call compatibility and replace direct approval consumption with `_approval_or_grant_for_review`.

5. In `_process_impl`, consume a grant before policy evaluation. If a grant exists, skip evaluation. If no grant exists, keep evaluation for direct-call compatibility and replace direct approval consumption with `_approval_or_grant_for_review`.

- [ ] **Step 4: Run terminal/process wrapper tests**

Run:

```bash
pytest tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 5: Run middleware tests to catch duplicate-consume regressions**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/terminal.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py
git commit -m "refactor: use policy grants in terminal tools"
```

---

## Task 4: Migrate Write File and Patch Approval Consumption

**Files:**
- Modify: `agent_tools/public/files.py`
- Modify: `tests/test_file_tools_hermes_env.py`

- [ ] **Step 1: Add failing file grant-path tests**

Add tests near the existing write/patch approval tests. Use the same file module helpers already present in that test file. The assertions must prove the wrapper uses a grant without consuming a raw approval:

```python
def test_write_file_uses_middleware_grant_for_review_path(monkeypatch):
    import json
    from types import SimpleNamespace

    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public import files

    calls = []
    thread_id = "write-grant-thread"
    tool_call_id = "call-write-grant"
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="write_file",
            risk_tags=("external_file_write",),
        )
    )

    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *args, **kwargs: files.PolicyDecision.review(
            "external_file_write",
            message="Approval required before writing outside the workspace.",
        ),
    )
    monkeypatch.setattr(
        files.file_policy,
        "approved_write_root_for_path",
        lambda path, **kwargs: "/tmp/approved",
    )
    monkeypatch.setattr(
        files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"success": True, "message": "File written."}),
    )

    raw = files._write_file_impl(path="/tmp/out.txt", content="ok", runtime=runtime)

    assert json.loads(raw)["ok"] is True
    assert calls[0]["approved_write_roots"] == ["/tmp/approved"]
```

Add the patch equivalent:

```python
def test_patch_uses_middleware_grant_for_review_path(monkeypatch):
    import json
    from types import SimpleNamespace

    from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public import files

    calls = []
    thread_id = "patch-grant-thread"
    tool_call_id = "call-patch-grant"
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=hermes_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="patch",
            risk_tags=("external_file_write",),
        )
    )

    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda *args, **kwargs: files.PolicyDecision.review(
            "external_file_write",
            message="Approval required before patching outside the workspace.",
        ),
    )
    monkeypatch.setattr(
        files.file_policy,
        "approved_write_root_for_path",
        lambda path, **kwargs: "/tmp/approved",
    )
    monkeypatch.setattr(
        files,
        "patch_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"success": True, "message": "Patch applied."}),
    )

    raw = files._patch_impl(
        mode="replace",
        path="/tmp/out.txt",
        old_string="old",
        new_string="new",
        runtime=runtime,
    )

    assert json.loads(raw)["ok"] is True
    assert calls[0]["approved_write_roots"] == ["/tmp/approved"]
```

- [ ] **Step 2: Run file tests to verify failure**

Run the specific test file containing the new tests:

```bash
pytest tests/test_file_tools_hermes_env.py -q
```

Expected: FAIL because file wrappers still consume raw approvals and ignore grants.

- [ ] **Step 3: Refactor file approval helpers**

In `agent_tools/public/files.py`:

1. Add imports:

```python
from agent_core.permissions.tool_grants import consume_tool_policy_grant
from agent_core.policy_tool_middleware import patch_policy_args, write_file_policy_args
```

2. In `_approval_roots_for_file_decision`, check grant first:

```python
    grant = consume_tool_policy_grant(
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
        tool_name=tool_name,
        required_risk_tags=decision.risk_tags,
    )
    if grant is None:
        approval = consume_approval(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name=tool_name,
            args=args,
            required_risk_tags=decision.risk_tags,
        )
        if approval is None:
            return "Approval required before writing outside the workspace."
```

3. In `_write_file_impl`, replace inline canonical args with:

```python
args=write_file_policy_args({"path": path, "content": content})
```

4. In `_patch_impl`, replace inline canonical args with:

```python
approval_args = patch_policy_args(
    {
        "mode": mode,
        "path": path,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
        "patch": patch,
    }
)
```

5. In `_write_file_impl` and `_patch_impl`, consume a middleware grant before the file tool-level policy consume path. Keep file path classification and approved root calculation in place. If no grant exists and the file decision is review, use the direct `consume_approval` fallback.

- [ ] **Step 4: Run file tests**

Run:

```bash
pytest tests/test_file_tools_hermes_env.py -q
```

Expected: PASS.

- [ ] **Step 5: Run middleware and terminal/process tests**

Run:

```bash
pytest tests/test_policy_tool_middleware.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/files.py tests/test_file_tools_hermes_env.py
git commit -m "refactor: use policy grants in file tools"
```

---

## Task 5: Wire PolicyToolMiddleware Into Agent Builders

**Files:**
- Modify: `agent_core/builders.py`
- Modify: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Add failing builder wiring test**

Append to `tests/test_policy_tool_middleware.py`:

```python
def test_build_agent_registers_policy_tool_middleware(monkeypatch):
    from agent_core import builders
    from agent_core.policy_tool_middleware import PolicyToolMiddleware

    captured = {}

    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda name: "")
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])
    monkeypatch.setattr(builders, "build_tool_call_limit_middleware", lambda include_task=True: [])
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: captured.update(kwargs) or "agent")

    assert builders.build_agent() == "agent"

    middleware = captured["middleware"]
    assert any(isinstance(item, PolicyToolMiddleware) for item in middleware)
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
pytest tests/test_policy_tool_middleware.py::test_build_agent_registers_policy_tool_middleware -q
```

Expected: FAIL because builders do not yet register `PolicyToolMiddleware`.

- [ ] **Step 3: Update builder wiring**

In `agent_core/builders.py`, add:

```python
from agent_core.policy_tool_middleware import PolicyToolMiddleware
```

Then insert immediately after `FlexibleHumanInTheLoopMiddleware(...)`:

```python
            PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS),
```

- [ ] **Step 4: Run builder test**

Run:

```bash
pytest tests/test_policy_tool_middleware.py::test_build_agent_registers_policy_tool_middleware -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/builders.py tests/test_policy_tool_middleware.py
git commit -m "feat: wire policy tool middleware"
```

---

## Task 6: Integration Regression and Cleanup

**Files:**
- Verify: `agent_core/policy_tool_middleware.py`, `agent_core/permissions/tool_grants.py`, `agent_tools/public/terminal.py`, `agent_tools/public/files.py`, `agent_core/builders.py`, and related tests.

- [ ] **Step 1: Run focused policy regression suite**

Run:

```bash
pytest tests/test_policy_tool_middleware.py tests/test_permissions_human_loop.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py tests/test_permissions_approvals.py tests/test_permissions_tool_policy.py tests/test_file_tools_hermes_env.py -q
```

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run:

```bash
python -m py_compile agent_core/policy_tool_middleware.py agent_core/permissions/tool_grants.py agent_tools/public/terminal.py agent_tools/public/files.py agent_core/builders.py
```

Expected: exits 0 with no output.

- [ ] **Step 3: Search for unintended duplicate primary approval paths**

Run:

```bash
rg -n "evaluate_tool_call|consume_approval|consume_tool_policy_grant" agent_core/policy_tool_middleware.py agent_tools/public/terminal.py agent_tools/public/files.py
```

Expected:

- `agent_core/policy_tool_middleware.py` contains the primary `evaluate_tool_call` and `consume_approval` path.
- `agent_tools/public/terminal.py` contains only compatibility fallback usage for direct wrapper calls.
- `agent_tools/public/files.py` contains only compatibility fallback usage plus file-specific path/root handling.

- [ ] **Step 4: Commit final cleanup when Step 1 or Step 2 required code changes**

If Step 1 or Step 2 required code changes, commit them:

```bash
git add agent_core agent_tools tests
git commit -m "fix: stabilize policy tool middleware migration"
```

If no files changed, do not create an empty commit.
