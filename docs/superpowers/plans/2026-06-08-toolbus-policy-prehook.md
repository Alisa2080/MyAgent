# ToolBus Policy Pre-Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move production execution-time policy enforcement into `ToolBusMiddleware` as a policy pre-hook while preserving approval, grant, and compatibility behavior.

**Architecture:** First lock the current nested `ToolBusMiddleware + PolicyToolMiddleware` behavior with integration tests. Then extract the policy gate into `agent_core/policy_tool_gate.py`, keep `PolicyToolMiddleware` as a compatibility wrapper, extend ToolBus blocked pre-hook lifecycle, and finally wire `build_agent()` to use `ToolBusHooks(pre_tool_call=[build_policy_pre_hook(...)])` instead of registering `PolicyToolMiddleware`.

**Tech Stack:** Python 3.11, LangChain `AgentMiddleware`, LangChain `ToolCallRequest`, LangChain Core `ToolMessage`, LangGraph `Command`, existing `RuntimeContext`, existing `tool_policy`, existing approval and grant stores, pytest, project conda interpreter `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## File Structure

- Create `tests/test_policy_tool_bus_integration.py`
  - Locks current nested ToolBus + Policy middleware behavior before migration.
  - Covers allow, deny, approval-required, approval-consumed, grant recording, and canonical digest compatibility.
- Create `agent_core/policy_tool_gate.py`
  - Owns `PolicyToolGateRequest`, `POLICY_ARG_BUILDERS`, canonical arg helper functions, `run_policy_tool_gate(...)`, and `build_policy_pre_hook(...)`.
  - This file is the shared policy gate used by both ToolBus and the compatibility middleware.
- Modify `agent_core/policy_tool_middleware.py`
  - Re-export policy arg builders for `human_loop.py` compatibility.
  - Delegate `_gate_tool_call(...)` to `run_policy_tool_gate(...)`.
- Create `tests/test_policy_tool_gate.py`
  - Unit-tests the shared gate directly, independent of LangChain middleware nesting.
- Modify `agent_core/tool_bus_middleware.py`
  - Add a blocked pre-hook finalization path that runs post hooks and returns directly.
  - Keep transform hooks and result limiting out of blocked pre-hook results.
- Modify `tests/test_tool_bus_middleware.py`
  - Locks the new blocked pre-hook lifecycle for sync and async wrappers.
- Modify `agent_core/builders.py`
  - Import `ToolBusHooks` and `build_policy_pre_hook`.
  - Wire policy enforcement into `ToolBusMiddleware`.
  - Remove `PolicyToolMiddleware(...)` from production middleware.
- Modify `tests/test_agent_cli_builders.py`
  - Assert builder wires a policy pre-hook into ToolBus and no longer registers `PolicyToolMiddleware`.
- Modify `tests/test_policy_tool_middleware.py`
  - Keep compatibility behavior tests.
  - Replace the builder registration assertion with a compatibility-wrapper assertion.

## Task 1: Lock Current Nested Policy + ToolBus Behavior

**Files:**
- Create: `tests/test_policy_tool_bus_integration.py`
- Read: `agent_core/tool_bus_middleware.py`
- Read: `agent_core/policy_tool_middleware.py`
- Read: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Write baseline integration tests for the current nested allow and deny paths**

Create `tests/test_policy_tool_bus_integration.py`:

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


def _run_nested_policy_toolbus(request, handler, *, post_calls: list | None = None):
    from agent_core.policy_tool_middleware import PolicyToolMiddleware
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    policy = PolicyToolMiddleware(policy_tools={"terminal", "process", "write_file", "patch"})

    hooks = ToolBusHooks()
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

    bus = ToolBusMiddleware(hooks=hooks)
    return bus.wrap_tool_call(request, lambda call_request: policy.wrap_tool_call(call_request, handler))


def test_nested_toolbus_policy_allows_handler_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args

    request = _request("terminal", {"command": "pwd"}, tool_call_id="call-allow")
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
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


def test_nested_toolbus_policy_deny_blocks_handler_and_posts_result():
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-deny"),
        post_calls=post_calls,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert post_calls == [("terminal", "error", "policy_denied")]
```

- [ ] **Step 2: Add baseline integration tests for approval-required and approval-consumed paths**

Append to `tests/test_policy_tool_bus_integration.py`:

```python
def test_nested_toolbus_policy_review_without_approval_blocks_handler():
    request = _request(
        "terminal",
        {"command": "touch approval-required.txt"},
        tool_call_id="call-review-missing",
    )
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-review-missing"),
        post_calls=post_calls,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "approval_required"
    assert post_calls == [("terminal", "error", "approval_required")]


def test_nested_toolbus_policy_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
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

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", tool_call_id),
    )

    assert result.artifact["ok"] is True
    assert calls == [request]

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

- [ ] **Step 3: Run integration tests and verify the current baseline passes**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_bus_integration.py -q
```

Expected: PASS. These tests lock existing behavior before refactoring.

- [ ] **Step 4: Commit baseline tests**

```bash
git add tests/test_policy_tool_bus_integration.py
git commit -m "test: lock policy and toolbus integration behavior"
```

## Task 2: Add Direct Policy Gate Tests

**Files:**
- Create: `tests/test_policy_tool_gate.py`
- Read: `agent_core/policy_tool_middleware.py`
- Read: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Write failing tests for direct gate allow, deny, and unsupported tools**

Create `tests/test_policy_tool_gate.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from agent_core.permissions.approvals import clear_approvals
from agent_core.permissions.tool_grants import clear_tool_policy_grants
from agent_core.session_context import runtime_task_id_from_thread_id


def setup_function():
    clear_approvals()
    clear_tool_policy_grants()


def teardown_function():
    clear_approvals()
    clear_tool_policy_grants()


def _runtime(thread_id: str = "policy-gate-thread", tool_call_id: str = "call-policy-gate"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _gate_request(tool_name: str, args: dict, *, tool_call_id: str = "call-policy-gate", runtime=None):
    from agent_core.policy_tool_gate import PolicyToolGateRequest

    return PolicyToolGateRequest(
        tool_name=tool_name,
        args=args,
        tool_call_id=tool_call_id,
        runtime=runtime or _runtime(tool_call_id=tool_call_id),
        request=None,
    )


def test_policy_tool_gate_allows_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "pwd"}, tool_call_id="call-allow"),
        policy_tools={"terminal"},
    )

    assert result is None
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-allow",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ()


def test_policy_tool_gate_denies_without_recording_grant():
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny"),
        policy_tools={"terminal"},
    )

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert (
        consume_tool_policy_grant(
            task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
            tool_call_id="call-deny",
            tool_name="terminal",
            args=canonical_tool_args("terminal", {"command": "rm -rf /"}),
        )
        is None
    )


def test_policy_tool_gate_ignores_unsupported_tool():
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("read_file", {"path": "README.md"}, tool_call_id="call-read"),
        policy_tools={"terminal"},
    )

    assert result is None
```

- [ ] **Step 2: Add failing tests for review approval and runtime tool-call-id fallback**

Append to `tests/test_policy_tool_gate.py`:

```python
def test_policy_tool_gate_requires_approval_for_review_decision():
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request(
            "terminal",
            {"command": "touch approval-required.txt"},
            tool_call_id="call-review",
        ),
        policy_tools={"terminal"},
    )

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "approval_required"


def test_policy_tool_gate_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-gate-network",
            decision_id="decision-gate-network",
            task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
            tool_call_id="call-network",
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    result = run_policy_tool_gate(
        _gate_request(
            "terminal",
            {"command": "curl https://example.com"},
            tool_call_id="call-network",
        ),
        policy_tools={"terminal"},
    )

    assert result is None
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-network",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ("network_access",)
    assert grant.allow_network_once is True


def test_policy_tool_gate_uses_runtime_tool_call_id_when_request_id_is_empty():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    runtime = _runtime(tool_call_id="call-from-runtime")

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "pwd"}, tool_call_id="", runtime=runtime),
        policy_tools={"terminal"},
    )

    assert result is None
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-from-runtime",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
```

- [ ] **Step 3: Run tests and verify they fail because the module does not exist**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_gate.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.policy_tool_gate'`.

- [ ] **Step 4: Commit failing gate tests**

```bash
git add tests/test_policy_tool_gate.py
git commit -m "test: define reusable policy gate behavior"
```

## Task 3: Extract the Shared Policy Gate

**Files:**
- Create: `agent_core/policy_tool_gate.py`
- Modify: `agent_core/policy_tool_middleware.py`
- Test: `tests/test_policy_tool_gate.py`
- Test: `tests/test_policy_tool_middleware.py`
- Test: `tests/test_policy_tool_bus_integration.py`

- [ ] **Step 1: Implement `agent_core/policy_tool_gate.py`**

Create `agent_core/policy_tool_gate.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval, make_args_digest
from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
from agent_core.session_context import RuntimeContext
from agent_tools.shared.tool_result import tool_failure


@dataclass(frozen=True)
class PolicyToolGateRequest:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str
    runtime: Any
    request: Any | None = None


def terminal_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("terminal", args)


def process_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("process", args)


def write_file_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("write_file", args)


def patch_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("patch", args)


POLICY_ARG_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "terminal": terminal_policy_args,
    "process": process_policy_args,
    "write_file": write_file_policy_args,
    "patch": patch_policy_args,
}


def run_policy_tool_gate(
    gate_request: PolicyToolGateRequest,
    *,
    policy_tools: set[str],
) -> ToolMessage | None:
    tool_name = gate_request.tool_name
    if tool_name not in policy_tools:
        return None

    builder = POLICY_ARG_BUILDERS.get(tool_name)
    if builder is None:
        return None

    runtime_context = RuntimeContext.from_runtime(gate_request.runtime)
    tool_call_id = gate_request.tool_call_id or runtime_context.tool_call_id
    policy_args = builder(gate_request.args or {})
    decision = tool_policy.evaluate_tool_call(
        tool_name,
        policy_args,
        runtime_context.task_id,
        tool_call_id=tool_call_id,
    )

    if decision.outcome == "deny":
        return tool_failure(
            tool_name,
            decision.human_message,
            code="policy_denied",
            data=decision.data,
            runtime=gate_request.runtime,
        )

    if decision.outcome == "allow":
        if tool_call_id:
            record_tool_policy_grant(
                ToolPolicyGrant(
                    task_id=runtime_context.task_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args_digest=make_args_digest(policy_args),
                    risk_tags=decision.risk_tags,
                )
            )
        return None

    approval = consume_approval(
        task_id=runtime_context.task_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=policy_args,
        required_risk_tags=decision.risk_tags,
    )
    if approval is None:
        return tool_failure(
            tool_name,
            decision.human_message,
            code="approval_required",
            data=decision.data,
            runtime=gate_request.runtime,
        )

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=runtime_context.task_id,
            tool_call_id=tool_call_id or "",
            tool_name=tool_name,
            args_digest=make_args_digest(policy_args),
            risk_tags=approval.risk_tags,
            allow_network_once=approval.allow_network_once,
        )
    )
    return None
```

- [ ] **Step 2: Update `PolicyToolMiddleware` to delegate to the shared gate**

Replace the top-level policy arg builder definitions and `_gate_tool_call(...)` logic in `agent_core/policy_tool_middleware.py` with imports and delegation:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_core.policy_tool_gate import (
    POLICY_ARG_BUILDERS,
    PolicyToolGateRequest,
    patch_policy_args,
    process_policy_args,
    run_policy_tool_gate,
    terminal_policy_args,
    write_file_policy_args,
)


class PolicyToolMiddleware(AgentMiddleware):
    def __init__(self, *, policy_tools: set[str]) -> None:
        super().__init__()
        self.policy_tools = set(policy_tools)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        gated = self._gate_tool_call(request)
        if gated is not None:
            return gated
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        gated = self._gate_tool_call(request)
        if gated is not None:
            return gated
        return await handler(request)

    def _gate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = request.tool_call
        raw_args = tool_call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {}
        return run_policy_tool_gate(
            PolicyToolGateRequest(
                tool_name=str(tool_call.get("name") or ""),
                args=args,
                tool_call_id=str(tool_call.get("id") or ""),
                runtime=request.runtime,
                request=request,
            ),
            policy_tools=self.policy_tools,
        )
```

- [ ] **Step 3: Run direct gate tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_gate.py -q
```

Expected: PASS.

- [ ] **Step 4: Run compatibility and integration tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_middleware.py tests/test_policy_tool_bus_integration.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit shared gate extraction**

```bash
git add agent_core/policy_tool_gate.py agent_core/policy_tool_middleware.py tests/test_policy_tool_gate.py
git commit -m "feat: extract reusable policy tool gate"
```

## Task 4: Define ToolBus Blocked Pre-Hook Lifecycle Tests

**Files:**
- Modify: `tests/test_tool_bus_middleware.py`
- Read: `agent_core/tool_bus_middleware.py`

- [ ] **Step 1: Add failing sync test for blocked pre-hook post-only lifecycle**

Append to `tests/test_tool_bus_middleware.py`:

```python
def test_pre_hook_blocked_result_triggers_post_but_skips_transform_and_limit():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    blocked = _message(content="blocked-" + ("x" * 80), status="error")
    post_calls = []
    transform_calls = []
    spec = ToolSpec(
        name="fake_tool",
        toolset="fake",
        tool=SimpleNamespace(name="fake_tool"),
        max_result_size_chars=10,
    )
    bus = ToolBusMiddleware(
        specs={"fake_tool": spec},
        hooks=ToolBusHooks(
            pre_tool_call=[lambda req: blocked],
            post_tool_call=[
                lambda req, result: post_calls.append(
                    (req.tool_name, result.result, result.duration_ms, result.error)
                )
            ],
            transform_tool_result=[
                lambda req, result: transform_calls.append(result) or _message(content="transformed")
            ],
        ),
    )
    calls = []

    result = bus.wrap_tool_call(
        _request(),
        lambda received: calls.append(received) or _message(content="handler"),
    )

    assert result is blocked
    assert calls == []
    assert len(post_calls) == 1
    assert post_calls[0][0] == "fake_tool"
    assert post_calls[0][1] is blocked
    assert isinstance(post_calls[0][2], int)
    assert post_calls[0][3] is None
    assert transform_calls == []
    assert result.content.startswith("blocked-")
    assert "truncated" not in result.content
```

- [ ] **Step 2: Add failing async test for the same lifecycle**

Append to `tests/test_tool_bus_middleware.py`:

```python
def test_async_pre_hook_blocked_result_triggers_post_but_skips_transform_and_limit():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    async def run():
        blocked = _message(content="blocked-" + ("x" * 80), status="error")
        post_calls = []
        transform_calls = []
        spec = ToolSpec(
            name="fake_tool",
            toolset="fake",
            tool=SimpleNamespace(name="fake_tool"),
            max_result_size_chars=10,
        )
        bus = ToolBusMiddleware(
            specs={"fake_tool": spec},
            hooks=ToolBusHooks(
                pre_tool_call=[lambda req: blocked],
                post_tool_call=[
                    lambda req, result: post_calls.append(
                        (req.tool_name, result.result, result.duration_ms, result.error)
                    )
                ],
                transform_tool_result=[
                    lambda req, result: transform_calls.append(result) or _message(content="transformed")
                ],
            ),
        )
        calls = []

        async def handler(received):
            calls.append(received)
            return _message(content="handler")

        result = await bus.awrap_tool_call(_request(), handler)

        assert result is blocked
        assert calls == []
        assert len(post_calls) == 1
        assert post_calls[0][0] == "fake_tool"
        assert post_calls[0][1] is blocked
        assert isinstance(post_calls[0][2], int)
        assert post_calls[0][3] is None
        assert transform_calls == []
        assert result.content.startswith("blocked-")
        assert "truncated" not in result.content

    asyncio.run(run())
```

- [ ] **Step 3: Run ToolBus tests and verify the new lifecycle tests fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_bus_middleware.py -q
```

Expected: FAIL because post hooks are not called for pre-hook blocked results.

- [ ] **Step 4: Commit failing ToolBus lifecycle tests**

```bash
git add tests/test_tool_bus_middleware.py
git commit -m "test: define toolbus blocked pre-hook lifecycle"
```

## Task 5: Implement ToolBus Blocked Pre-Hook Lifecycle

**Files:**
- Modify: `agent_core/tool_bus_middleware.py`
- Test: `tests/test_tool_bus_middleware.py`

- [ ] **Step 1: Refactor post hook execution and blocked finalization**

In `agent_core/tool_bus_middleware.py`, add `_run_post_hooks(...)` and `_finalize_blocked(...)`, and update `_finalize(...)` to call the shared post helper:

```python
    def _run_post_hooks(self, bus_request: ToolBusRequest, bus_result: ToolBusResult) -> None:
        for hook in self.hooks.post_tool_call:
            try:
                hook(bus_request, bus_result)
            except Exception:
                logger.warning("ToolBus post hook failed", exc_info=True)

    def _finalize_blocked(
        self,
        bus_request: ToolBusRequest,
        result: ToolMessage,
        duration_ms: int,
    ) -> ToolMessage:
        self._run_post_hooks(
            bus_request,
            ToolBusResult(result=result, duration_ms=duration_ms, error=None),
        )
        return result

    def _finalize(
        self,
        bus_request: ToolBusRequest,
        result: ToolResponse,
        duration_ms: int,
        error: Exception | None,
    ) -> ToolResponse:
        bus_result = ToolBusResult(result=result, duration_ms=duration_ms, error=error)
        self._run_post_hooks(bus_request, bus_result)

        if not isinstance(result, ToolMessage):
            return result

        transformed = self._run_transform_hooks(bus_request, result)
        limited = self._limit_result(bus_request.spec, transformed)
        return limited
```

- [ ] **Step 2: Use blocked finalization in sync and async wrappers**

Change both blocked branches in `wrap_tool_call(...)` and `awrap_tool_call(...)`:

```python
            blocked = self._run_pre_hooks(bus_request)
            if blocked is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                return self._finalize_blocked(bus_request, blocked, duration_ms)
```

- [ ] **Step 3: Run ToolBus tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_bus_middleware.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit ToolBus lifecycle implementation**

```bash
git add agent_core/tool_bus_middleware.py tests/test_tool_bus_middleware.py
git commit -m "feat: run post hooks for blocked toolbus pre-hooks"
```

## Task 6: Add ToolBus Policy Pre-Hook Tests

**Files:**
- Modify: `tests/test_policy_tool_gate.py`
- Read: `agent_core/policy_tool_gate.py`
- Read: `agent_core/tool_bus_middleware.py`

- [ ] **Step 1: Add failing tests for `build_policy_pre_hook(...)` deny and approval-required paths**

Append to `tests/test_policy_tool_gate.py`:

```python
def _toolbus_request(tool_name: str, args: dict, *, tool_call_id: str = "call-toolbus-policy", runtime=None):
    from agent_core.tool_bus_middleware import ToolBusRequest

    return ToolBusRequest(
        tool_name=tool_name,
        args=args,
        tool_call_id=tool_call_id,
        runtime=runtime or _runtime(tool_call_id=tool_call_id),
        request=None,
        spec=None,
    )


def test_policy_pre_hook_denies_toolbus_request():
    from agent_core.policy_tool_gate import build_policy_pre_hook

    hook = build_policy_pre_hook(policy_tools={"terminal"})

    result = hook(_toolbus_request("terminal", {"command": "rm -rf /"}, tool_call_id="call-hook-deny"))

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "policy_denied"


def test_policy_pre_hook_requires_approval_for_review_toolbus_request():
    from agent_core.policy_tool_gate import build_policy_pre_hook

    hook = build_policy_pre_hook(policy_tools={"terminal"})

    result = hook(
        _toolbus_request(
            "terminal",
            {"command": "touch approval-required.txt"},
            tool_call_id="call-hook-review",
        )
    )

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "approval_required"
```

- [ ] **Step 2: Add failing tests for pre-hook allow and approval-consumed paths**

Append to `tests/test_policy_tool_gate.py`:

```python
def test_policy_pre_hook_allows_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import build_policy_pre_hook

    hook = build_policy_pre_hook(policy_tools={"terminal"})

    result = hook(_toolbus_request("terminal", {"command": "pwd"}, tool_call_id="call-hook-allow"))

    assert result is None
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-hook-allow",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)


def test_policy_pre_hook_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import build_policy_pre_hook

    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-hook-network",
            decision_id="decision-hook-network",
            task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
            tool_call_id="call-hook-network",
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )
    hook = build_policy_pre_hook(policy_tools={"terminal"})

    result = hook(
        _toolbus_request(
            "terminal",
            {"command": "curl https://example.com"},
            tool_call_id="call-hook-network",
        )
    )

    assert result is None
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-hook-network",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.risk_tags == ("network_access",)
    assert grant.allow_network_once is True
```

- [ ] **Step 3: Run tests and verify they fail because `build_policy_pre_hook` is missing**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_gate.py -q
```

Expected: FAIL with `ImportError` for `build_policy_pre_hook`.

- [ ] **Step 4: Commit failing pre-hook tests**

```bash
git add tests/test_policy_tool_gate.py
git commit -m "test: define policy toolbus pre-hook behavior"
```

## Task 7: Implement `build_policy_pre_hook(...)`

**Files:**
- Modify: `agent_core/policy_tool_gate.py`
- Test: `tests/test_policy_tool_gate.py`
- Test: `tests/test_policy_tool_bus_integration.py`

- [ ] **Step 1: Add ToolBus imports and hook builder**

Append this function to `agent_core/policy_tool_gate.py` and add imports under `TYPE_CHECKING` to avoid runtime cycles:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.tool_bus_middleware import PreToolHook, ToolBusRequest
```

```python
def build_policy_pre_hook(*, policy_tools: set[str]) -> "PreToolHook":
    configured_policy_tools = set(policy_tools)

    def hook(bus_request: "ToolBusRequest") -> ToolMessage | None:
        return run_policy_tool_gate(
            PolicyToolGateRequest(
                tool_name=bus_request.tool_name,
                args=bus_request.args,
                tool_call_id=bus_request.tool_call_id,
                runtime=bus_request.runtime,
                request=bus_request.request,
            ),
            policy_tools=configured_policy_tools,
        )

    return hook
```

- [ ] **Step 2: Run policy gate tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_gate.py -q
```

Expected: PASS.

- [ ] **Step 3: Run integration baseline tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_bus_integration.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit pre-hook implementation**

```bash
git add agent_core/policy_tool_gate.py tests/test_policy_tool_gate.py
git commit -m "feat: add policy toolbus pre-hook"
```

## Task 8: Wire Builder to ToolBus Policy Pre-Hook

**Files:**
- Modify: `agent_core/builders.py`
- Modify: `tests/test_agent_cli_builders.py`
- Modify: `tests/test_policy_tool_middleware.py`
- Test: `tests/test_agent_cli_builders.py`
- Test: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Update builder tests to expect ToolBus policy hook wiring**

In `tests/test_agent_cli_builders.py`, replace `test_build_agent_includes_tool_bus_before_policy` with:

```python
def test_build_agent_wires_policy_pre_hook_into_toolbus(monkeypatch):
    import agent_core.builders as builders

    class FakeToolBus:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(builders, "PolicyToolMiddleware", FakePolicy, raising=False)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: kwargs)

    agent_config = builders.build_agent()
    middleware = agent_config["middleware"]
    tool_bus_items = [item for item in middleware if isinstance(item, FakeToolBus)]
    policy_items = [item for item in middleware if isinstance(item, FakePolicy)]

    assert len(tool_bus_items) == 1
    assert policy_items == []
    assert tool_bus_items[0].kwargs["specs"]
    assert "terminal" in tool_bus_items[0].kwargs["specs"]
    hooks = tool_bus_items[0].kwargs["hooks"]
    assert len(hooks.pre_tool_call) == 1
    assert hooks.pre_tool_call[0].__name__ == "hook"
```

- [ ] **Step 2: Update read-only builder order test**

In `tests/test_agent_cli_builders.py`, update `test_build_agent_wires_read_only_before_toolbus_and_policy` to remove `FakePolicy` lookup and assert read-only remains before ToolBus:

```python
def test_build_agent_wires_read_only_before_toolbus(monkeypatch):
    import agent_core.builders as builders

    class FakeReadOnlyLimit:
        def __init__(self, *, specs):
            self.specs = specs

    class FakeToolBus:
        def __init__(self, *, specs, hooks):
            self.specs = specs
            self.hooks = hooks

    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(builders, "create_agent", fake_create_agent)
    monkeypatch.setattr(builders, "ConsecutiveReadOnlyToolLimitMiddleware", FakeReadOnlyLimit)
    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])

    builders.build_agent()

    middleware = captured["middleware"]
    read_only_index = next(
        index for index, item in enumerate(middleware)
        if isinstance(item, FakeReadOnlyLimit)
    )
    toolbus_index = next(
        index for index, item in enumerate(middleware)
        if isinstance(item, FakeToolBus)
    )

    assert read_only_index < toolbus_index
    assert middleware[read_only_index].specs is middleware[toolbus_index].specs
    assert len(middleware[toolbus_index].hooks.pre_tool_call) == 1
```

- [ ] **Step 3: Update policy middleware builder-specific test**

In `tests/test_policy_tool_middleware.py`, replace `test_build_agent_registers_policy_tool_middleware` with:

```python
def test_policy_tool_middleware_remains_available_as_compatibility_wrapper():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("read_file", {"path": "README.md"}, tool_call_id="call-compat-read")
    calls = []

    result = middleware.wrap_tool_call(
        request,
        lambda received: calls.append(received)
        or ToolMessage(
            content="File read.",
            name="read_file",
            tool_call_id="call-compat-read",
            status="success",
            artifact={
                "ok": True,
                "tool": "read_file",
                "message": "File read.",
                "data": None,
                "error": None,
                "meta": {},
            },
        ),
    )

    assert result.artifact["ok"] is True
    assert calls == [request]
```

- [ ] **Step 4: Run builder and policy middleware tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_builders.py tests/test_policy_tool_middleware.py -q
```

Expected: FAIL because `build_agent()` still registers `PolicyToolMiddleware` and does not pass policy hooks to `ToolBusMiddleware`.

- [ ] **Step 5: Commit failing builder migration tests**

```bash
git add tests/test_agent_cli_builders.py tests/test_policy_tool_middleware.py
git commit -m "test: define builder policy pre-hook wiring"
```

## Task 9: Implement Builder Policy Pre-Hook Wiring

**Files:**
- Modify: `agent_core/builders.py`
- Test: `tests/test_agent_cli_builders.py`
- Test: `tests/test_policy_tool_middleware.py`
- Test: `tests/test_policy_tool_gate.py`

- [ ] **Step 1: Update imports in `agent_core/builders.py`**

Replace:

```python
from agent_core.policy_tool_middleware import PolicyToolMiddleware
from agent_core.tool_bus_middleware import ToolBusMiddleware
```

with:

```python
from agent_core.policy_tool_gate import build_policy_pre_hook
from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
```

- [ ] **Step 2: Wire the policy hook into ToolBus and remove production Policy middleware**

Replace:

```python
            ToolBusMiddleware(specs=tool_specs),
            PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS),
```

with:

```python
            ToolBusMiddleware(
                specs=tool_specs,
                hooks=ToolBusHooks(
                    pre_tool_call=[
                        build_policy_pre_hook(policy_tools=POLICY_REVIEW_TOOLS),
                    ],
                ),
            ),
```

- [ ] **Step 3: Run builder and policy tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_builders.py tests/test_policy_tool_middleware.py tests/test_policy_tool_gate.py -q
```

Expected: PASS.

- [ ] **Step 4: Run integration and ToolBus tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_bus_integration.py tests/test_tool_bus_middleware.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit builder migration**

```bash
git add agent_core/builders.py tests/test_agent_cli_builders.py tests/test_policy_tool_middleware.py
git commit -m "feat: wire policy enforcement through toolbus pre-hook"
```

## Task 10: Final Verification

**Files:**
- Read: `docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md`
- Verify: all files modified in Tasks 1-9

- [ ] **Step 1: Run the targeted suite from the design**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_policy_tool_middleware.py \
  tests/test_policy_tool_gate.py \
  tests/test_tool_bus_middleware.py \
  tests/test_policy_tool_bus_integration.py \
  tests/test_permissions_human_loop.py \
  tests/test_agent_cli_builders.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run broader permission wrapper tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_terminal_wrappers.py \
  tests/test_permissions_process_wrappers.py \
  tests/test_permissions_file_wrappers.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run formatting-sensitive diff check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 4: Inspect final changed files**

Run:

```bash
git status --short
git diff --stat
```

Expected: only intentional files from this plan are modified.

- [ ] **Step 5: Commit stabilization fixes only when the verification commands required code changes**

If any verification command failed, first fix the failing code or tests, then rerun the failing command until it passes. After the fix passes, commit the stabilization changes:

```bash
git add agent_core tests
git commit -m "fix: stabilize toolbus policy pre-hook migration"
```

When all verification commands passed on the first run, skip this step without creating a commit.
