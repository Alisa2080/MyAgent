# ToolBus Policy Pre-Hook Design

Date: 2026-06-08

## Goal

Move execution-time policy enforcement from a standalone
`PolicyToolMiddleware` production path into `ToolBusMiddleware` as a pre-tool
hook, while preserving the existing approval and grant semantics.

The original migration covered three implementation stages:

1. Lock the original `ToolBusMiddleware + PolicyToolMiddleware` integration
   behavior with tests.
2. Extract policy execution gating into a reusable pure gate.
3. Add a ToolBus policy pre-hook and switch `build_agent()` to use it.

The current effective architecture also includes the follow-up deletion in
`2026-06-09-remove-policy-tool-middleware-design.md`: the old standalone
execution-time policy middleware module is removed, and production policy
enforcement runs only through the ToolBus policy pre-hook.

## Context

The project has two active governance layers around tool execution:

- `FlexibleHumanInTheLoopMiddleware` runs after the model response and before
  tool execution. It inspects proposed tool calls, interrupts for human review
  when policy says `review`, and records `ApprovalRecord` values after approval.
- `ToolBusMiddleware` runs at tool execution time. It prepares normalized
  tool requests, runs pre hooks, calls the LangChain handler, normalizes
  exceptions, runs post hooks, transforms `ToolMessage` results, and applies
  result limits.

LangChain's official middleware model supports this split. Human-in-the-loop
middleware uses `after_model` to pause after a model proposes tool calls and
before execution. Custom `wrap_tool_call` middleware is the official interception
point around tool execution. The project uses those same phases.

The ownership issue addressed by this migration was that production
execution-time policy enforcement used to be a separate `wrap_tool_call`
middleware nested with ToolBus. Since policy gating is a pre-execution tool-bus
concern, the current production path is owned by ToolBus pre hooks.

## Non-Goals

- Do not replace `FlexibleHumanInTheLoopMiddleware`.
- Do not change `tool_policy.evaluate_tool_call(...)` semantics.
- Do not broaden policy coverage beyond the existing policy tools:
  `terminal`, `process`, `write_file`, and `patch`.
- Do not change public tool schemas.
- Do not redesign approval or grant storage.
- Do not make ToolBus responsible for deciding which model-proposed calls should
  interrupt for human review.

## Recommended Design

The staged migration was:

1. Add integration tests that document the original combined behavior of
   `ToolBusMiddleware` wrapping `PolicyToolMiddleware`.
2. Extract policy enforcement into a reusable gate that is independent of
   LangChain middleware nesting.
3. Add a ToolBus pre-hook adapter for that gate.
4. Change `build_agent()` so production execution-time policy enforcement runs
   through `ToolBusMiddleware(hooks=...)`.
5. Remove the old standalone execution-time policy middleware path.

This keeps behavior testable throughout the migration and avoids a large
delete-and-rewrite change.

## Architecture

The target production path is:

```text
build_agent()
  -> FlexibleHumanInTheLoopMiddleware   # after_model review selection
  -> ToolBusMiddleware                  # execution-time tool governance
       -> policy pre-hook               # execution-time policy gate
       -> handler(request)              # LangChain tool execution
       -> post hooks
       -> transform hooks
       -> result limits
```

`FlexibleHumanInTheLoopMiddleware` remains responsible for per-call review
selection and approval recording. `ToolBusMiddleware` becomes the single
production entry point for execution-time tool governance.

## Policy Gate Component

Add a reusable request type and gate function in a new module:
`agent_core/policy_tool_gate.py`.

```python
@dataclass(frozen=True)
class PolicyToolGateRequest:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str
    runtime: Any
    request: Any | None = None
```

```python
def run_policy_tool_gate(
    gate_request: PolicyToolGateRequest,
    *,
    policy_tools: set[str],
) -> ToolMessage | None:
    ...
```

The gate owns the execution-time sequence originally implemented by the removed
middleware:

1. Ignore tools that are not configured policy tools.
2. Ignore tools without a policy arg builder.
3. Resolve `RuntimeContext` from the runtime.
4. Use the tool-call id from the gate request, falling back to runtime context.
5. Build canonical policy args with `POLICY_ARG_BUILDERS`.
6. Evaluate `tool_policy.evaluate_tool_call(...)`.
7. Return `policy_denied` for deny decisions.
8. For allow decisions, record a `ToolPolicyGrant` and allow execution.
9. For review decisions, consume a matching `ApprovalRecord`.
10. Return `approval_required` when review approval is missing.
11. Record a `ToolPolicyGrant` after approval consumption and allow execution.

The approval digest and grant digest must remain based on canonical policy args,
not raw request args. This keeps the execution-time gate compatible with the
approval records produced by `FlexibleHumanInTheLoopMiddleware`.

## ToolBus Pre-Hook Adapter

Add a hook builder:

```python
def build_policy_pre_hook(*, policy_tools: set[str]) -> PreToolHook:
    def hook(bus_request: ToolBusRequest) -> ToolMessage | None:
        return run_policy_tool_gate(
            PolicyToolGateRequest(
                tool_name=bus_request.tool_name,
                args=bus_request.args,
                tool_call_id=bus_request.tool_call_id,
                runtime=bus_request.runtime,
                request=bus_request.request,
            ),
            policy_tools=policy_tools,
        )
    return hook
```

The hook uses `ToolBusRequest.args`, which are prepared before pre hooks run.
That means execution-time policy evaluates the arguments that ToolBus will pass
to the actual tool handler, after ToolBus schema coercion when coercion is
enabled.

This is intentional: execution-time policy should gate the real execution
payload. Tests must prove that approval consumption still matches approval
records produced by the HITL phase.

## Current Design Decision

The migration originally kept `PolicyToolMiddleware` as a temporary compatibility
wrapper. The follow-up deletion design
`2026-06-09-remove-policy-tool-middleware-design.md` removes that wrapper, so
`agent_core.policy_tool_gate` is the only execution-time policy gate module and
production enforcement runs only through the ToolBus policy pre-hook.

## Blocked Result Semantics

The target ToolBus policy pre-hook behavior is:

- If a pre hook returns `None`, ToolBus executes the handler normally.
- If a pre hook returns a `ToolMessage`, ToolBus treats the tool call as blocked.
- A blocked result triggers `post_tool_call` hooks.
- A blocked result is returned directly after post hooks.
- A blocked result does not run `transform_tool_result` hooks.
- A blocked result does not run result limiting.

This was an intentional behavior change from the old middleware nesting, where a
policy-blocked `ToolMessage` could be seen by ToolBus as the handler result and
therefore flow through the existing finalize path. The new semantics better
separate execution observation from result shaping: post hooks may audit that a
tool was blocked, while safety and approval errors remain unmodified.

## Stage 0: Lock Original Behavior

Add integration tests in `tests/test_policy_tool_bus_integration.py`.

At that stage, tests exercised `ToolBusMiddleware` combined with
`PolicyToolMiddleware` before any production wiring changes:

- `allow`: handler is called, result succeeds, grant is recorded, and ToolBus
  post hooks observe the successful result.
- `deny`: handler is not called and the result has error code `policy_denied`.
- `review` without approval: handler is not called and the result has error code
  `approval_required`.
- `review` with approval: approval is consumed, handler is called, and a
  matching `ToolPolicyGrant` is recorded.
- digest compatibility: approval records produced with canonical policy args can
  be consumed by the execution-time gate.

This stage is a baseline. It should not change implementation behavior.

## Stage 1: Extract the Pure Policy Gate

This historical stage extracted shared behavior before the old middleware module
was deleted by the follow-up design.

Implementation tasks:

- Add `agent_core/policy_tool_gate.py`.
- Define `PolicyToolGateRequest`.
- Move or share `POLICY_ARG_BUILDERS`.
- Implement `run_policy_tool_gate(...)`.
- During the intermediate migration, update `PolicyToolMiddleware` to delegate
  to the gate.
- During the intermediate migration, keep the existing `PolicyToolMiddleware`
  public behavior unchanged.
- Keep `human_loop.py` policy arg behavior unchanged.

Tests:

- Add direct gate tests for allow, deny, review without approval, review with
  approval, unsupported tool pass-through, and tool-call-id fallback.

Success criteria:

- Artifact error codes remain unchanged.
- Grant digest and approval digest behavior remains unchanged.
- `allow_network_once` is preserved for network approvals.
- No production builder wiring changes are required in this stage.

## Stage 2: Add the ToolBus Policy Pre-Hook

Implementation tasks:

- Add `build_policy_pre_hook(...)`.
- Extend `ToolBusMiddleware` so a blocked pre-hook result triggers post hooks and
  returns directly.
- Ensure blocked pre-hook results skip transform hooks and result limits.
- Update `build_agent()` to construct `ToolBusHooks` with the policy pre-hook.
- Remove `PolicyToolMiddleware(...)` from `build_agent()` production middleware.
- Delete the old standalone execution-time policy middleware module.

The builder target shape is:

```python
ToolBusMiddleware(
    specs=tool_specs,
    hooks=ToolBusHooks(
        pre_tool_call=[
            build_policy_pre_hook(policy_tools=POLICY_REVIEW_TOOLS),
        ],
    ),
)
```

Tests:

- ToolBus pre-hook deny blocks handler execution.
- ToolBus pre-hook approval-required blocks handler execution.
- Blocked policy results trigger post hooks exactly once.
- Blocked policy results do not trigger transform hooks.
- Blocked policy results do not run result limiting.
- Allow path still executes the handler and then uses normal post, transform, and
  result-limit behavior.
- Approval-consumed path consumes the approval and records a grant.
- Sync and async ToolBus wrappers behave consistently.
- Builder tests assert that production wiring uses ToolBus policy hooks and no
  longer registers `PolicyToolMiddleware`.

Success criteria:

- Production execution-time policy enforcement runs through ToolBus pre hooks.
- HITL approval recording still happens in `FlexibleHumanInTheLoopMiddleware`.
- The old execution-time middleware entry point is removed.
- The new blocked-result lifecycle is locked by tests.

## Data Flow

Review path:

1. The model emits tool calls.
2. `FlexibleHumanInTheLoopMiddleware.after_model()` evaluates each policy tool
   call.
3. Calls with outcome `review` interrupt for human input.
4. Human approval records an `ApprovalRecord` keyed by task id and tool-call id.
5. Tool execution enters `ToolBusMiddleware.wrap_tool_call()`.
6. ToolBus prepares `ToolBusRequest`.
7. The policy pre-hook evaluates the actual execution args.
8. The gate consumes the matching approval and records `ToolPolicyGrant`.
9. ToolBus executes the handler.
10. Tool-specific execution can consume the grant when needed.

Deny path:

1. Tool execution enters ToolBus.
2. The policy pre-hook evaluates the call.
3. The gate returns `policy_denied`.
4. ToolBus runs post hooks with the blocked result.
5. ToolBus returns the blocked `ToolMessage` directly.

Approval-required path:

1. Tool execution enters ToolBus.
2. The policy pre-hook evaluates the call as `review`.
3. No matching approval exists.
4. The gate returns `approval_required`.
5. ToolBus runs post hooks with the blocked result.
6. ToolBus returns the blocked `ToolMessage` directly.

## Error Handling

Policy-denied and approval-required messages should continue to use
`tool_failure(...)` and the existing artifact format.

Hook exceptions in ToolBus continue to be logged and ignored. A failure in a
policy pre-hook should follow the existing ToolBus pre-hook behavior unless the
implementation explicitly chooses to fail closed. This design does not change
the general hook exception policy.

Unexpected exceptions inside the shared policy gate should be treated as
implementation bugs and surfaced through the calling middleware's existing
error-handling path. Tests should focus on supported policy outcomes rather than
asserting broad exception swallowing.

## Testing Strategy

Run targeted tests after each stage:

```text
tests/test_policy_tool_gate.py
tests/test_tool_bus_middleware.py
tests/test_policy_tool_bus_integration.py
tests/test_permissions_human_loop.py
tests/test_agent_cli_builders.py
```

When practical, also run the broader permission wrapper tests for terminal,
process, and file tools because those wrappers consume policy grants after the
execution-time gate.

## Success Criteria

- `ToolBusMiddleware` is the only production execution-time policy enforcement
  middleware registered by `build_agent()`.
- `FlexibleHumanInTheLoopMiddleware` remains the only component responsible for
  after-model human review selection and approval recording.
- The shared policy gate preserves existing allow, deny, review, approval
  consumption, grant recording, digest, and `allow_network_once` behavior.
- Policy-blocked ToolBus pre-hook results trigger post hooks and skip
  transform/result limiting.
- The old `PolicyToolMiddleware` module has been removed; policy behavior is
  covered by gate, ToolBus, integration, and builder tests.
- Tests document both the migration baseline and the final ToolBus pre-hook
  behavior.
