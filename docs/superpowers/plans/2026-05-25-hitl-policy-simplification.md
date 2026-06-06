# HITL Policy Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `FlexibleHumanInTheLoopMiddleware.after_model` a thin documented adapter, preserve dynamic policy review behavior, and keep `PolicyToolMiddleware` as the only execution-time policy gate.

**Architecture:** The installed LangChain `HumanInTheLoopMiddleware.after_model` only selects interrupts by tool name, so it cannot safely express per-call dynamic policy decisions when two calls to the same tool have different outcomes in the same model turn. The implementation will document that blocker, keep the smallest fallback loop, move the copied official flow behind `_after_model_minimal_fallback`, and make `after_model` itself a thin wrapper. Policy review selection will reuse `PolicyToolMiddleware` policy arg builders to avoid drift.

**Tech Stack:** Python 3.11, LangChain `HumanInTheLoopMiddleware`, LangGraph `interrupt`, pytest, existing `tool_policy`, `PolicyToolMiddleware`, approval registry, and audit helpers.

---

## File Structure

- Modify `agent_core/human_loop.py`
  - Add an explicit `OFFICIAL_AFTER_MODEL_BLOCKER` constant that documents why the current LangChain official `after_model` cannot be used as the full implementation.
  - Import `POLICY_ARG_BUILDERS` from `agent_core.policy_tool_middleware`.
  - Add `_policy_args_for_tool_call` so policy review selection and approval recording use the same canonical args as execution-time gating.
  - Make `after_model` a thin wrapper that delegates to `_after_model_minimal_fallback`.
  - Move the current fallback body into `_after_model_minimal_fallback`.
  - Remove the custom `commands` interrupt config extension because official HITL does not support it and the project config does not use it.

- Modify `tests/test_permissions_human_loop.py`
  - Add mixed same-tool tests that prove official tool-name-only `interrupt_on` is insufficient for this project.
  - Add structural tests that keep `after_model` thin and prevent policy deny/deferred logic from returning.
  - Add tests proving policy arg builders are reused by `human_loop`.

- No production changes to `agent_core/policy_tool_middleware.py`
  - It already owns execution-time allow/deny/review gates and exposes `POLICY_ARG_BUILDERS`.

Use this interpreter for all Python and pytest commands:

```bash
/home/miku/miniforge3/envs/langchain/bin/python
```

---

### Task 1: Lock In the Per-Call Dynamic Review Blocker

**Files:**
- Modify: `tests/test_permissions_human_loop.py`

- [ ] **Step 1: Add a mixed same-tool regression test**

Add this test after `test_policy_deny_does_not_interrupt` in `tests/test_permissions_human_loop.py`:

```python
def test_mixed_same_tool_policy_only_interrupts_review_call(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import runtime_task_id_from_thread_id

    clear_approvals()
    seen_payloads = []

    def fake_interrupt(payload):
        seen_payloads.append(payload)
        return {"type": "approve"}

    def fake_evaluate_tool_call(**kwargs):
        if kwargs["args"]["command"] == "pwd":
            return PolicyDecision.allow("read_tool")
        return PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        )

    monkeypatch.setattr(human_loop, "interrupt", fake_interrupt)
    monkeypatch.setattr(human_loop.tool_policy, "evaluate_tool_call", fake_evaluate_tool_call)

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pwd"}, "id": "allow-call"},
            {
                "name": "terminal",
                "args": {"command": "pip install rich"},
                "id": "review-call",
            },
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert len(seen_payloads) == 1
    action_requests = seen_payloads[0]["action_requests"]
    assert [request["args"]["command"] for request in action_requests] == [
        "pip install rich"
    ]
    assert [tool_call["id"] for tool_call in result["messages"][0].tool_calls] == [
        "allow-call",
        "review-call",
    ]

    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id("thread-1"),
            tool_call_id="allow-call",
            tool_name="terminal",
            args=_terminal_policy_args("pwd"),
            required_risk_tags=("package_install",),
        )
        is None
    )
    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id("thread-1"),
            tool_call_id="review-call",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is not None
    )
```

- [ ] **Step 2: Run the new test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_permissions_human_loop.py::test_mixed_same_tool_policy_only_interrupts_review_call -v
```

Expected: PASS on the current fallback implementation. This test documents the behavior that a plain official `interrupt_on["terminal"]` wrapper would get wrong.

- [ ] **Step 3: Commit the blocker regression test**

Run:

```bash
git add tests/test_permissions_human_loop.py
git commit -m "test: cover mixed policy HITL review selection"
```

---

### Task 2: Reuse PolicyToolMiddleware Canonical Arg Builders

**Files:**
- Modify: `tests/test_permissions_human_loop.py`
- Modify: `agent_core/human_loop.py`

- [ ] **Step 1: Add failing tests for policy arg builder reuse**

Add these tests after `test_mixed_same_tool_policy_only_interrupts_review_call`:

```python
def test_policy_review_selection_uses_policy_arg_builder(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    seen_args = []

    def fake_builder(args):
        return {"command": args["command"], "builder_marker": "from-builder"}

    def fake_evaluate_tool_call(**kwargs):
        seen_args.append(kwargs["args"])
        return PolicyDecision.allow("read_tool")

    monkeypatch.setitem(human_loop.POLICY_ARG_BUILDERS, "terminal", fake_builder)
    monkeypatch.setattr(human_loop.tool_policy, "evaluate_tool_call", fake_evaluate_tool_call)
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: (_ for _ in ()).throw(AssertionError("should not interrupt")),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pwd"}, "id": "call-builder"}
        ],
    )

    assert middleware.after_model({"messages": [message]}, _runtime()) is None
    assert seen_args == [{"command": "pwd", "builder_marker": "from-builder"}]


def test_policy_approval_digest_uses_policy_arg_builder(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import runtime_task_id_from_thread_id

    clear_approvals()

    def fake_builder(args):
        return {"command": args["command"], "builder_marker": "from-builder"}

    monkeypatch.setitem(human_loop.POLICY_ARG_BUILDERS, "terminal", fake_builder)
    monkeypatch.setattr(human_loop, "interrupt", lambda _payload: {"type": "approve"})
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "terminal",
                "args": {"command": "pip install rich"},
                "id": "call-builder",
            }
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id("thread-1"),
            tool_call_id="call-builder",
            tool_name="terminal",
            args={"command": "pip install rich", "builder_marker": "from-builder"},
            required_risk_tags=("package_install",),
        )
        is not None
    )
    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id("thread-1"),
            tool_call_id="call-builder",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is None
    )
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_human_loop.py::test_policy_review_selection_uses_policy_arg_builder \
  tests/test_permissions_human_loop.py::test_policy_approval_digest_uses_policy_arg_builder -v
```

Expected: FAIL because `agent_core.human_loop` does not yet expose `POLICY_ARG_BUILDERS`.

- [ ] **Step 3: Import arg builders and add a helper**

In `agent_core/human_loop.py`, add this import:

```python
from agent_core.policy_tool_middleware import POLICY_ARG_BUILDERS
```

Then add this method to `FlexibleHumanInTheLoopMiddleware` before `_policy_decision_for_tool_call`:

```python
    @staticmethod
    def _policy_args_for_tool_call(tool_call: ToolCall) -> dict[str, Any]:
        args = tool_call.get("args") or {}
        builder = POLICY_ARG_BUILDERS.get(tool_call["name"])
        if builder is None:
            return tool_policy.canonical_tool_args(tool_call["name"], args)
        return builder(args)
```

- [ ] **Step 4: Use the helper for policy evaluation**

Replace `_policy_decision_for_tool_call` in `agent_core/human_loop.py` with:

```python
    @classmethod
    def _policy_decision_for_tool_call(cls, tool_call: ToolCall, runtime: Runtime[Any]) -> Any:
        task_id = runtime_task_id_from_runtime(runtime)
        return tool_policy.evaluate_tool_call(
            tool_name=tool_call["name"],
            args=cls._policy_args_for_tool_call(tool_call),
            task_id=task_id,
            tool_call_id=tool_call.get("id"),
        )
```

- [ ] **Step 5: Use the helper for approval digests**

In `_record_policy_approval`, replace:

```python
                args_digest=make_args_digest(
                    tool_policy.canonical_tool_args(
                        tool_call["name"],
                        tool_call.get("args") or {},
                    )
                ),
```

with:

```python
                args_digest=make_args_digest(self._policy_args_for_tool_call(tool_call)),
```

- [ ] **Step 6: Run the focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_human_loop.py::test_policy_review_selection_uses_policy_arg_builder \
  tests/test_permissions_human_loop.py::test_policy_approval_digest_uses_policy_arg_builder -v
```

Expected: PASS.

- [ ] **Step 7: Commit the arg builder reuse**

Run:

```bash
git add agent_core/human_loop.py tests/test_permissions_human_loop.py
git commit -m "fix: reuse policy arg builders in HITL review"
```

---

### Task 3: Make `after_model` a Thin Documented Fallback Wrapper

**Files:**
- Modify: `tests/test_permissions_human_loop.py`
- Modify: `agent_core/human_loop.py`

- [ ] **Step 1: Add structural tests for the thin wrapper and documented blocker**

Add these imports near the top of `tests/test_permissions_human_loop.py`:

```python
import inspect
```

Add these tests near the end of the file:

```python
def test_after_model_is_thin_wrapper_around_documented_fallback():
    import agent_core.human_loop as human_loop

    source = inspect.getsource(human_loop.FlexibleHumanInTheLoopMiddleware.after_model)

    assert "_after_model_minimal_fallback" in source
    assert "revised_tool_calls" not in source
    assert "decision_idx" not in source
    assert "artificial_tool_messages" not in source
    assert "policy_denied" not in source
    assert "tool_call_deferred" not in source


def test_official_after_model_blocker_is_documented():
    import agent_core.human_loop as human_loop

    blocker = human_loop.FlexibleHumanInTheLoopMiddleware.OFFICIAL_AFTER_MODEL_BLOCKER

    assert "tool-name" in blocker
    assert "per-call" in blocker
    assert "same tool" in blocker
```

- [ ] **Step 2: Run the structural tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_human_loop.py::test_after_model_is_thin_wrapper_around_documented_fallback \
  tests/test_permissions_human_loop.py::test_official_after_model_blocker_is_documented -v
```

Expected: FAIL because `after_model` still contains the fallback body and `OFFICIAL_AFTER_MODEL_BLOCKER` does not exist.

- [ ] **Step 3: Add the documented blocker constant**

Inside `FlexibleHumanInTheLoopMiddleware`, immediately after the class docstring, add:

```python
    OFFICIAL_AFTER_MODEL_BLOCKER = (
        "LangChain HumanInTheLoopMiddleware.after_model selects interrupted calls "
        "by tool-name interrupt_on entries, but this project needs per-call policy "
        "review selection. A single model turn can contain two calls to the same tool "
        "with different policy outcomes, such as terminal allow and terminal review. "
        "Using official after_model directly would interrupt both calls."
    )
```

- [ ] **Step 4: Move the current body into `_after_model_minimal_fallback`**

Rename the current `after_model` method in `agent_core/human_loop.py` to:

```python
    def _after_model_minimal_fallback(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
```

Keep the method body unchanged in this step.

- [ ] **Step 5: Add the thin `after_model` wrapper**

Add this method above `_after_model_minimal_fallback`:

```python
    def after_model(self, state: dict[str, Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Run the smallest fallback around official HITL helpers.

        The official after_model implementation cannot be used directly because
        it supports interrupt selection by tool name, not by individual tool call.
        See OFFICIAL_AFTER_MODEL_BLOCKER for the concrete mixed-call case.
        """
        return self._after_model_minimal_fallback(state, runtime)
```

Do not change `aafter_model`; it should keep calling `self.after_model(state, runtime)`.

- [ ] **Step 6: Run the structural tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_human_loop.py::test_after_model_is_thin_wrapper_around_documented_fallback \
  tests/test_permissions_human_loop.py::test_official_after_model_blocker_is_documented -v
```

Expected: PASS.

- [ ] **Step 7: Commit the wrapper extraction**

Run:

```bash
git add agent_core/human_loop.py tests/test_permissions_human_loop.py
git commit -m "refactor: document HITL after_model fallback"
```

---

### Task 4: Remove Non-Official Static Interrupt Extensions

**Files:**
- Modify: `agent_core/human_loop.py`
- Modify: `tests/test_permissions_human_loop.py`

- [ ] **Step 1: Add a regression assertion for static non-policy HITL**

Update `test_non_policy_tool_still_uses_interrupt_on` so the interrupt payload is asserted explicitly:

```python
    action_requests = seen_payloads[0]["action_requests"]
    assert len(action_requests) == 1
    assert action_requests[0]["name"] == "memory_manage"
    assert action_requests[0]["description"] == "Review memory change."
```

Place these assertions after `assert seen_payloads`.

- [ ] **Step 2: Run the static HITL test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_permissions_human_loop.py::test_non_policy_tool_still_uses_interrupt_on -v
```

Expected: PASS before implementation.

- [ ] **Step 3: Remove `_resolve_interrupt_config`**

Delete this method from `agent_core/human_loop.py`:

```python
    @staticmethod
    def _resolve_interrupt_config(tool_call: ToolCall, interrupt_on: dict[str, Any]) -> Any:
        config = interrupt_on.get(tool_call["name"])
        if not isinstance(config, dict):
            return config

        commands = config.get("commands")
        if not commands:
            return config

        tool_args = tool_call.get("args") or {}
        if tool_args.get("command") not in commands:
            return None

        return {key: value for key, value in config.items() if key != "commands"}
```

- [ ] **Step 4: Use official static interrupt lookup semantics**

In `_after_model_minimal_fallback`, replace:

```python
                config = self._resolve_interrupt_config(tool_call, self.interrupt_on)
                if config is None:
                    continue
```

with:

```python
                config = self.interrupt_on.get(tool_call["name"])
                if config is None:
                    continue
```

- [ ] **Step 5: Run the static HITL test again**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_permissions_human_loop.py::test_non_policy_tool_still_uses_interrupt_on -v
```

Expected: PASS.

- [ ] **Step 6: Commit the static interrupt cleanup**

Run:

```bash
git add agent_core/human_loop.py tests/test_permissions_human_loop.py
git commit -m "refactor: align static HITL interrupt lookup"
```

---

### Task 5: Prevent Policy Deny and Deferred ToolMessage Regression

**Files:**
- Modify: `tests/test_permissions_human_loop.py`

- [ ] **Step 1: Add structural regression tests**

Add this test near the structural tests from Task 3:

```python
def test_human_loop_does_not_own_policy_deny_or_deferred_messages():
    import agent_core.human_loop as human_loop

    source = inspect.getsource(human_loop.FlexibleHumanInTheLoopMiddleware)

    assert "policy_denied" not in source
    assert "tool_call_deferred" not in source
    assert "_tool_message_for_denial" not in source
    assert "_tool_message_for_deferred_call" not in source
    assert "_complete_artificial_tool_messages" not in source
```

- [ ] **Step 2: Run the regression test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_permissions_human_loop.py::test_human_loop_does_not_own_policy_deny_or_deferred_messages -v
```

Expected: PASS.

- [ ] **Step 3: Commit the regression test**

Run:

```bash
git add tests/test_permissions_human_loop.py
git commit -m "test: prevent HITL policy deny regression"
```

---

### Task 6: Run Targeted Behavior Verification

**Files:**
- No file changes expected.

- [ ] **Step 1: Run all human-loop tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_permissions_human_loop.py -v
```

Expected: PASS.

- [ ] **Step 2: Run policy middleware tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_policy_tool_middleware.py -v
```

Expected: PASS.

- [ ] **Step 3: Run builder/config smoke tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_terminal_tools.py::test_human_interrupt_intercepts_terminal_and_process \
  tests/test_policy_tool_middleware.py::test_build_agent_registers_policy_tool_middleware -v
```

Expected: PASS.

- [ ] **Step 4: Inspect the final diff**

Run:

```bash
git diff -- agent_core/human_loop.py tests/test_permissions_human_loop.py
```

Expected:

- `after_model` is a thin wrapper.
- `_after_model_minimal_fallback` contains the remaining copied official loop.
- `OFFICIAL_AFTER_MODEL_BLOCKER` documents the installed LangChain API limitation.
- policy arg builders are reused for evaluation and approval digests.
- no `policy_denied`, `tool_call_deferred`, denial tool-message helper, or deferred tool-message helper exists in `agent_core/human_loop.py`.

- [ ] **Step 5: Commit verification-only cleanup if needed**

If Step 4 reveals formatting-only cleanup, make that cleanup and run:

```bash
git add agent_core/human_loop.py tests/test_permissions_human_loop.py
git commit -m "test: verify HITL fallback boundaries"
```

If no cleanup is needed, do not create an empty commit.

---

## Self-Review Notes

- Spec coverage:
  - Dynamic policy review selection is covered by Task 1.
  - Reuse of canonical policy arg builders is covered by Task 2.
  - Thin `after_model` and documented fallback are covered by Task 3.
  - Official static `interrupt_on` alignment is covered by Task 4.
  - Deny/deferred regression prevention is covered by Task 5.
  - Behavior verification is covered by Task 6.
- Plan hygiene scan: no incomplete markers or open-ended implementation steps remain.
- Type consistency:
  - `Runtime[Any]`, `ToolCall`, and `dict[str, Any]` match existing `agent_core/human_loop.py`.
  - `POLICY_ARG_BUILDERS` exists in `agent_core.policy_tool_middleware`.
  - Approval consumption tests use existing `consume_approval` and `runtime_task_id_from_thread_id`.
