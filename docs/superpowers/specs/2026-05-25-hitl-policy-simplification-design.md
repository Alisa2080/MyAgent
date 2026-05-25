# HITL Policy Simplification Design

Date: 2026-05-25

## Goal

Reduce the maintenance surface of `agent_core.human_loop.FlexibleHumanInTheLoopMiddleware` by making LangChain's official `HumanInTheLoopMiddleware.after_model` the owner of the human-review state machine again.

The project-specific human-loop layer should only provide the behavior that LangChain does not provide directly:

- lenient LangSmith resume value parsing
- dynamic selection of policy-controlled tool calls that need human review
- approval and audit recording after a human approves a policy review

`PolicyToolMiddleware` remains the final execution-time policy gate for `terminal`, `process`, `write_file`, and `patch`.

## Non-Goals

- Do not change public tool schemas.
- Do not change `tool_policy` decision semantics.
- Do not change approval record storage or single-use consumption semantics.
- Do not move file path classification or approved-root calculation into the human loop.
- Do not make `FlexibleHumanInTheLoopMiddleware` responsible for execution-time deny/review/allow gates.
- Do not implement session-wide or time-window approvals.

## Current Problem

`FlexibleHumanInTheLoopMiddleware` currently inherits from LangChain's official `HumanInTheLoopMiddleware`, but it reimplements most of the official `after_model` flow:

- find the latest `AIMessage`
- inspect tool calls
- build action requests and review configs
- call `interrupt`
- normalize and validate human decisions
- process `approve`, `edit`, `reject`, and `respond`
- mutate the AI message's tool calls
- synthesize `ToolMessage` instances

The copied flow also mixes project policy behavior into the same method:

- evaluate `tool_policy` for policy-controlled tools
- audit policy decisions
- synthesize `policy_denied` messages
- create deferred tool messages for mixed policy cases
- record approvals after human approval

This makes the middleware fragile across LangChain upgrades. If LangChain changes the HITL request shape, decision handling, or message update behavior, this project must manually notice and port those changes.

## Recommended Design

Use a thin extension around official HITL behavior.

`FlexibleHumanInTheLoopMiddleware` should continue to inherit from `HumanInTheLoopMiddleware`, but it should stop owning the full HITL state machine. The implementation must first attempt to make custom `after_model` a thin wrapper around `super().after_model(state, runtime)`. A copied decision loop is allowed only after the implementation proves that the installed LangChain API cannot safely support the required dynamic review injection, lenient resume normalization, or approve-side-effect capture.

The intended responsibilities are:

1. Before official HITL interrupt selection, dynamically identify policy-controlled tool calls whose policy outcome is `review`.
2. Present those calls to official HITL using the same `allowed_decisions` contract as static `interrupt_on`.
3. Normalize lenient LangSmith resume values into the official `{"decisions": [...]}` shape.
4. After an approved policy review, record the project approval and audit metadata.

All final execution-time policy enforcement belongs to `PolicyToolMiddleware`.

## Preferred Implementation Path

Use a super-wrapper design.

`FlexibleHumanInTheLoopMiddleware.after_model`, if it exists at all, should do only these steps:

1. Build a per-call policy review context for the current model turn.
2. Temporarily expose policy `review` decisions as official HITL interrupt configs.
3. Run `super().after_model(state, runtime)` so LangChain owns request construction, decision validation, tool-call rebuilding, and artificial `ToolMessage` creation.
4. Capture normalized human decisions from the official interrupt path.
5. Record approval and audit side effects for policy review decisions whose human decision is `approve`.
6. Restore any temporary state before returning.

The method should not contain a custom loop that rebuilds `revised_tool_calls`, advances `decision_idx`, or appends `artificial_tool_messages`. Those are official HITL responsibilities.

The implementation should use the least invasive adapter available in the installed LangChain version. If a scoped interrupt adapter is needed to normalize resume values and capture decisions, it must be local to the current middleware call and must not leave global monkeypatch state behind.

## Architecture

### Official HITL Middleware

LangChain's `HumanInTheLoopMiddleware` remains responsible for:

- action request creation
- review config creation
- interrupt payload structure
- decision count validation
- `approve`, `edit`, `reject`, and `respond` processing
- AI message tool-call updates
- artificial `ToolMessage` creation for reject/respond decisions

The project should not duplicate these mechanics unless the installed LangChain version lacks the hook needed for dynamic review selection, response normalization, or decision capture.

### Flexible Human Loop

`FlexibleHumanInTheLoopMiddleware` becomes a narrow adapter.

It keeps:

- `_unwrap_response`
- `_normalize_response`
- policy review config construction
- approval record creation
- audit logging

It removes ownership of:

- policy deny handling
- deferred tool-call messages
- execution-time policy enforcement
- duplicated decision processing

If a policy decision is:

- `allow`: do not interrupt; let the tool call proceed to `PolicyToolMiddleware`
- `deny`: do not interrupt; let `PolicyToolMiddleware` return `policy_denied`
- `review`: include the tool call in the HITL interrupt request

### PolicyToolMiddleware

`PolicyToolMiddleware` is the final gate for policy-controlled tools.

It continues to:

- evaluate canonical policy args at tool execution time
- return `policy_denied` for deny decisions
- consume recorded approvals for review decisions
- return `approval_required` when review approval is missing
- record per-call grants when execution is authorized

This means edited tool calls are intentionally re-evaluated at execution time. An edit does not inherit approval from the original reviewed arguments. If the edited call still requires review, `PolicyToolMiddleware` fails closed with `approval_required`.

## Dynamic Review Selection

The dynamic review selector evaluates only tools configured as policy-controlled:

- `terminal`
- `process`
- `write_file`
- `patch`

For each policy-controlled tool call, it evaluates `tool_policy.evaluate_tool_call` using the same canonical argument form consumed by `PolicyToolMiddleware`. The selector should reuse the policy arg builders from `agent_core.policy_tool_middleware` or a shared module so the after-model review decision and execution-time gate cannot drift.

The selector must not consume approvals or record execution grants. It only decides whether a human review prompt should be shown.

Only `review` decisions become HITL action requests. The review description should come from the policy decision's human-facing message, and allowed decisions should remain:

- `approve`
- `edit`
- `reject`
- `respond`

Policy decisions should be audited when evaluated for review selection. Approvals should be audited again when the human approves.

## Approval Recording

Approval records are created only when all of these are true:

- the interrupted tool call came from a policy `review` decision
- the human decision type is `approve`
- the approved tool call still has a tool call id

Approval records must bind to:

- task id
- tool call id
- tool name
- digest of canonical policy args
- risk tags from the original policy decision
- one-shot network allowance from the original policy decision

No approval is recorded for:

- `reject`
- `respond`
- `edit`
- policy `allow`
- policy `deny`
- static non-policy HITL tools such as memory or skill management

For `edit`, the edited call proceeds without a recorded approval. `PolicyToolMiddleware` re-evaluates it later and requires a matching approval if needed.

## Lenient Resume Handling

The middleware should continue accepting LangSmith resume variants that users or clients may send in practice:

- `{"decisions": [{"type": "approve"}]}`
- `[{"type": "approve"}]`
- `{"type": "approve"}`
- nested wrappers such as `{"value": ...}`, `{"input": ...}`, `{"response": ...}`, or `{"resume": ...}`
- scalar shortcuts such as `"approve"`, `"yes"`, `"0"`, `"reject"`, `"no"`, or `"1"`

Internally, these should be normalized into the official decision list shape before official decision processing runs.

## Fallback Boundary

The preferred implementation is a thin integration with official `HumanInTheLoopMiddleware.after_model`. The implementation must try this path first.

If the current LangChain API does not expose enough hooks to add dynamic review selection, normalize resume values, or capture approved policy decisions around `super().after_model`, the fallback is a smaller custom `after_model` that:

- delegates action/config creation to official `_create_action_and_config`
- delegates decision handling to official `_process_decision`
- handles only dynamic policy review selection, lenient response normalization, and approval/audit recording

The fallback must still avoid reintroducing policy deny handling, deferred tool messages, or execution-time policy enforcement.

When using the fallback, the implementation must document the specific blocker in code or in the implementation plan. Acceptable blockers include:

- no safe way to pass dynamic per-call interrupt configs into official `after_model`
- no safe way to normalize nonstandard resume values before official decision processing
- no safe way to know which captured decision corresponds to which policy review after official processing

The fallback is not acceptable merely because it is easier to implement.

## Error Handling

The human loop should fail closed for malformed human responses:

- invalid response shape raises the existing normalization error
- decision count mismatch raises the official-style count mismatch error
- unsupported decision type is delegated to official decision validation

Execution-time policy failures are not human-loop errors:

- policy deny is returned by `PolicyToolMiddleware` as `policy_denied`
- missing review approval is returned by `PolicyToolMiddleware` as `approval_required`

## Testing Strategy

Update or add tests that prove:

- policy `allow` does not trigger HITL and reaches `PolicyToolMiddleware`
- policy `deny` does not trigger HITL and is denied by `PolicyToolMiddleware`
- policy `review` triggers HITL
- approving a policy review records approval and audit metadata
- rejecting/responding to a policy review does not record approval
- editing a policy review does not record approval for edited args
- static non-policy HITL tools still use `interrupt_on`
- lenient resume forms still work
- async behavior matches sync behavior
- builder ordering keeps `FlexibleHumanInTheLoopMiddleware` before `PolicyToolMiddleware`
- `FlexibleHumanInTheLoopMiddleware` does not synthesize `policy_denied` or deferred tool messages
- if the super-wrapper path is implemented, official `HumanInTheLoopMiddleware.after_model` is invoked for interrupted calls
- if fallback remains, tests make the copied responsibilities explicit and prevent policy deny/deferred logic from returning

Tests should avoid depending on copied LangChain internals beyond the public or protected methods the project intentionally uses.

## Migration Plan

1. Add or adjust tests to encode the desired super-wrapper behavior.
2. Inspect the installed LangChain `HumanInTheLoopMiddleware.after_model` and identify the narrowest safe hook for dynamic interrupt config, lenient resume normalization, and decision capture.
3. Attempt the super-wrapper implementation first.
4. If the super-wrapper is blocked, document the blocker and keep only the smallest fallback loop that delegates to official `_create_action_and_config` and `_process_decision`.
5. Keep approval/audit recording only around approved policy reviews.
6. Verify the existing `PolicyToolMiddleware` tests still cover deny/review/allow execution behavior.
7. Remove tests that assert human-loop ownership of policy deny or deferred tool messages.

## Success Criteria

- `FlexibleHumanInTheLoopMiddleware` no longer owns execution-time policy deny/review/allow enforcement.
- `PolicyToolMiddleware` is the only policy-controlled execution gate for `terminal`, `process`, `write_file`, and `patch`.
- Official LangChain `HumanInTheLoopMiddleware.after_model` owns decision processing, tool-call rebuilding, and artificial `ToolMessage` creation unless a documented LangChain API blocker makes a minimal fallback necessary.
- `FlexibleHumanInTheLoopMiddleware.after_model` is removed or reduced to a thin wrapper around `super().after_model(state, runtime)`.
- The implementation does not contain a custom `revised_tool_calls` / `decision_idx` / `artificial_tool_messages` loop unless it is part of the documented fallback.
- LangSmith resume compatibility is preserved.
- Approval and audit recording semantics are preserved for approved policy reviews.
- Public tool schemas and user-visible approval semantics remain stable.
