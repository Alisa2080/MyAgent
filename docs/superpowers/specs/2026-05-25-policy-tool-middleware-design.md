# Policy Tool Middleware Design

Date: 2026-05-25

## Goal

Reduce duplicated tool approval and execution-gating logic by introducing a project-level `PolicyToolMiddleware` that uses LangChain's `wrap_tool_call` middleware hook.

The first implementation phase covers approval consumption for:

- `terminal`
- `process`
- `write_file`
- `patch`

This design keeps the existing human review request flow intact. `FlexibleHumanInTheLoopMiddleware` continues to evaluate model-proposed tool calls after model output, interrupt for human review, and record approvals. `PolicyToolMiddleware` is responsible for the execution-time gate: evaluate the same canonical policy input, consume recorded approvals for review decisions, deny execution when required, and pass execution to the underlying tool when allowed.

## Non-Goals

- Do not replace `FlexibleHumanInTheLoopMiddleware`.
- Do not move file path classification or approved write-root calculation into middleware in the first phase.
- Do not redesign `tool_policy`, `file_policy`, or approval storage.
- Do not change public tool schemas.
- Do not broaden policy coverage beyond `terminal`, `process`, `write_file`, and `patch`.

## Current Problem

The current tool wrappers contain repeated execution-gating logic:

- `agent_tools/public/terminal.py` normalizes terminal and process policy args, evaluates `tool_policy`, consumes approval for review decisions, converts deny/review failures into `tool_error`, and then calls reference implementation.
- `agent_tools/public/files.py` repeats the review approval path for `write_file` and `patch`, while also handling file-specific path classification and approved write roots.

This duplication makes it easy for behavior to drift across tools. A future policy change must be updated in several wrappers, and each wrapper must remember the same sequence: derive runtime identity, evaluate policy, consume approval, return a standard error on deny, then execute.

LangChain's custom middleware API supports `wrap_tool_call`, which can run logic before and after a tool call and can short-circuit by returning a `ToolMessage` instead of invoking the handler. That matches the execution-gating portion of this project.

## Recommended Design

Use a hybrid design:

1. Add `PolicyToolMiddleware` as the shared execution-time policy gate.
2. Add a small per-call grant abstraction for data that a wrapper needs after middleware consumes approval.
3. Keep tool-specific business execution in the wrappers.
4. Keep file-specific path classification and approved-root calculation inside file wrappers.

This avoids overloading middleware with file-system details while still moving the repeated approval consumption flow to one place.

## Components

### `PolicyToolMiddleware`

`PolicyToolMiddleware` wraps configured tool calls through `wrap_tool_call`.

For supported tools, it will:

1. Build canonical policy args for the tool.
2. Resolve `RuntimeContext` from the request runtime.
3. Evaluate `tool_policy.evaluate_tool_call`.
4. Return a standard tool error immediately for deny decisions.
5. For review decisions, consume the recorded approval once.
6. Store a short-lived per-call execution grant when the call is allowed to proceed.
7. Invoke the LangChain handler for allowed calls.

For unsupported tools, it simply invokes the handler.

### Policy Arg Builders

Move or expose the existing canonical arg builders so middleware can evaluate the exact same inputs the wrappers currently evaluate:

- `terminal`: existing terminal policy args
- `process`: existing process policy args
- `write_file`: file write policy args needed for tool-level approval consumption
- `patch`: patch policy args needed for tool-level approval consumption

The arg builder output must stay compatible with `FlexibleHumanInTheLoopMiddleware`, otherwise the after-model review decision and execution-time consume decision could diverge.

### Per-Call Grant

Add a small grant object keyed by `task_id` and `tool_call_id`.

The grant records that `PolicyToolMiddleware` already authorized this exact tool call. For review decisions, it also records that middleware already consumed the human approval. The first implementation stores:

- tool name
- decision/risk metadata needed by wrappers
- terminal-specific network allowance

Wrappers use this grant to skip duplicate policy evaluation, avoid consuming the same approval twice, and apply execution-specific behavior. When a wrapper is satisfying a review decision, it must only consume a grant whose risk tags cover the wrapper's required risk tags.

For direct tool implementation calls that bypass middleware, wrappers keep a compatibility fallback to the existing approval consume path. That fallback stays small and isolated so the production path remains middleware-driven.

## Tool Behavior

### `terminal`

Middleware handles policy evaluation, deny, and review approval consumption.

The wrapper keeps reference implementation execution. If a review grant exists, the wrapper applies the same behavior as today:

- `force=True`
- `allow_network_once=True` when the approval permits it

The wrapper no longer owns the main policy decision path.

### `process`

Middleware handles policy evaluation, deny, and review approval consumption.

The wrapper keeps reference implementation/process execution. Process approval has no new execution-specific side effect in this phase, so the wrapper becomes business execution plus result normalization.

### `write_file`

Middleware handles tool-level policy evaluation and approval consumption.

The wrapper keeps:

- file path classification through `file_policy`
- approved write-root calculation
- low-level file write execution
- result normalization

If middleware consumed approval, the wrapper reads the grant and computes approved roots from the file-specific decision. If middleware was bypassed, the wrapper uses the compatibility fallback.

### `patch`

Middleware handles tool-level policy evaluation and approval consumption.

The wrapper keeps:

- patch path classification
- approved write-root calculation
- low-level patch execution
- result normalization

The same grant/fallback behavior as `write_file` applies.

## Error Handling

`PolicyToolMiddleware` produces the same standard error shapes the wrappers currently return:

- deny returns a standard `tool_error`
- missing approval for a review decision returns the current approval-required error
- malformed policy args or missing runtime identity fail closed with a standard tool error

The migration must preserve observable behavior unless a test documents an intentional message change.

## Middleware Ordering

`FlexibleHumanInTheLoopMiddleware` remains responsible for after-model review requests and approval recording.

`PolicyToolMiddleware` runs at tool execution time and consumes the approval recorded by the human loop. Builder wiring must ensure both middleware layers are registered for agents using policy-controlled tools.

The implementation plan must include tests that prove an approval recorded by the human loop can be consumed by `PolicyToolMiddleware`.

## Testing Strategy

Add or update tests for:

- `terminal` allow, deny, approved review, and missing approval review
- `process` allow, deny, approved review, and missing approval review
- `write_file` approved review with approved roots still computed by wrapper
- `patch` approved review with approved roots still computed by wrapper
- direct wrapper compatibility path when middleware is bypassed
- builder wiring includes `PolicyToolMiddleware`
- existing `FlexibleHumanInTheLoopMiddleware` behavior remains unchanged

Regression tests assert both behavior and duplicate-consumption prevention.

## Success Criteria

- The repeated execution-time sequence for supported tools is centralized in `PolicyToolMiddleware`.
- Terminal/process wrappers no longer duplicate the main evaluate/consume/deny gate.
- File wrappers no longer own primary approval consumption, but still own file-specific path and root logic.
- Human review request behavior remains unchanged.
- Existing public tool schemas remain unchanged.
- Tests cover the middleware path and compatibility fallback.
