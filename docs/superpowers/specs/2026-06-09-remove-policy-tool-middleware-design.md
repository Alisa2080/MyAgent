# Remove PolicyToolMiddleware Design

Date: 2026-06-09

## Goal

Complete the ToolBus policy pre-hook migration by deleting the old
`PolicyToolMiddleware` entry point immediately. After this change,
execution-time policy enforcement should have exactly one active path:
`ToolBusMiddleware` running the policy pre-hook built by
`build_policy_pre_hook(...)`.

## Context

The current code already implements the main ToolBus policy pre-hook migration:

- `agent_core.policy_tool_gate` owns canonical policy arg builders,
  `run_policy_tool_gate(...)`, and `build_policy_pre_hook(...)`.
- `ToolBusMiddleware` runs blocked pre-hook results through post hooks, then
  returns them directly without transform hooks or result limiting.
- `build_agent()` wires policy enforcement through
  `ToolBusHooks(pre_tool_call=[build_policy_pre_hook(...)])`.
- `FlexibleHumanInTheLoopMiddleware` imports `POLICY_ARG_BUILDERS` from
  `agent_core.policy_tool_gate`.

The remaining issue is that `agent_core.policy_tool_middleware` still exists as
a compatibility wrapper. The project no longer wants a compatibility period.

## Decision

Delete `PolicyToolMiddleware` now.

This means:

- Delete `agent_core/policy_tool_middleware.py`.
- Do not keep a shim module.
- Do not keep a deprecated class that raises at runtime.
- Move current project imports of arg builders to `agent_core.policy_tool_gate`.
- Remove middleware-specific tests and keep behavior coverage in gate, ToolBus,
  and integration tests.

The immediate breakage for external code importing
`agent_core.policy_tool_middleware` is intentional.

## Non-Goals

- Do not change `tool_policy.evaluate_tool_call(...)`.
- Do not broaden policy coverage beyond `terminal`, `process`, `write_file`, and
  `patch`.
- Do not redesign approval or grant storage.
- Do not rewrite historical specs and plans. Old design documents may still
  mention `PolicyToolMiddleware` as historical context.
- Do not keep any runtime compatibility wrapper for `PolicyToolMiddleware`.

## Target Architecture

Policy ownership after deletion:

```text
FlexibleHumanInTheLoopMiddleware
  -> after_model review selection
  -> records ApprovalRecord
  -> uses POLICY_ARG_BUILDERS from policy_tool_gate

ToolBusMiddleware
  -> prepares ToolBusRequest
  -> runs build_policy_pre_hook(...)
  -> blocked policy result runs post hooks only
  -> allowed result reaches handler

policy_tool_gate
  -> terminal/process/write_file/patch canonical arg builders
  -> run_policy_tool_gate(...)
  -> build_policy_pre_hook(...)
```

There is no standalone LangChain `AgentMiddleware` for policy enforcement after
this change.

## Runtime Migration

Delete:

- `agent_core/policy_tool_middleware.py`

Update imports:

- `agent_tools/public/terminal.py`
  - Change `process_policy_args` and `terminal_policy_args` imports to
    `agent_core.policy_tool_gate`.
- `agent_tools/public/files.py`
  - Change `patch_policy_args` and `write_file_policy_args` imports to
    `agent_core.policy_tool_gate`.

Keep unchanged:

- `agent_core/builders.py`
  - It already wires policy enforcement through ToolBus hooks.
- `agent_core/human_loop.py`
  - It already imports `POLICY_ARG_BUILDERS` from `policy_tool_gate`.
- `agent_core/policy_tool_gate.py`
  - It remains the single policy gate module.

## Test Migration

Delete:

- `tests/test_policy_tool_middleware.py`

Replace old middleware integration:

- Update `tests/test_policy_tool_bus_integration.py` so it no longer imports or
  constructs `PolicyToolMiddleware`.
- The integration tests should use:

```python
ToolBusMiddleware(
    hooks=ToolBusHooks(
        pre_tool_call=[
            build_policy_pre_hook(policy_tools={"terminal", "process", "write_file", "patch"}),
        ],
    ),
)
```

The integration suite should keep coverage for:

- allow: handler runs, result succeeds, grant is recorded, post hook observes
  success.
- deny: handler does not run, result has `policy_denied`, post hook observes the
  blocked result.
- review without approval: handler does not run, result has
  `approval_required`, post hook observes the blocked result.
- review with approval: approval is consumed, handler runs, matching grant is
  recorded.
- blocked policy results: transform hooks do not run and result limits do not
  modify the safety/approval error.

Update tests that import arg builders from the old module:

- `tests/test_permissions_terminal_wrappers.py`
- `tests/test_permissions_process_wrappers.py`
- `tests/test_file_tools_terminal_env.py`

Those imports should point to `agent_core.policy_tool_gate`.

Keep and rely on:

- `tests/test_policy_tool_gate.py`
  - Unit coverage for allow, deny, approval-required, approval-consumed,
    unsupported tool pass-through, tool-call-id fallback, and ToolBus pre-hook
    adapter behavior.
- `tests/test_tool_bus_middleware.py`
  - Lifecycle coverage for blocked pre-hook post-only finalization.
- `tests/test_agent_cli_builders.py`
  - Builder coverage proving production ToolBus receives the policy pre-hook.
- `tests/test_permissions_human_loop.py`
  - HITL review selection and approval recording coverage.

## Current Documentation Scope

Update the current effective ToolBus policy design document:

- `docs/superpowers/specs/2026-06-08-toolbus-policy-prehook-design.md`

It should no longer say:

- `PolicyToolMiddleware` remains as a compatibility wrapper.
- `agent_core.policy_tool_middleware` remains an import path.
- `tests/test_policy_tool_middleware.py` is part of the target verification
  suite.

It should say:

- The old middleware module is deleted.
- `agent_core.policy_tool_gate` is the only policy gate module.
- Production enforcement is ToolBus pre-hook only.

Do not update older historical design or plan documents unless they are the
current effective docs for this migration.

## Risks

- External code importing `agent_core.policy_tool_middleware` will break. This is
  accepted as part of the immediate deletion decision.
- Deleting `tests/test_policy_tool_middleware.py` can hide behavior regressions
  if equivalent coverage is missing. The integration and gate tests must cover
  all former behavior before deletion.
- Stray imports of old arg builders can break terminal, process, or file wrapper
  tests. Wrapper tests must be part of the verification suite.
- Historical docs will still mention `PolicyToolMiddleware`; this is accepted to
  avoid broad documentation churn.

## Verification

Required checks:

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

Search checks:

```bash
rg -n "PolicyToolMiddleware|agent_core\\.policy_tool_middleware|policy_tool_middleware" \
  agent_core agent_tools tests
```

Expected: no matches.

Also run:

```bash
git diff --check
```

Expected: no output.

## Success Criteria

- `agent_core/policy_tool_middleware.py` no longer exists.
- Current runtime code and tests do not reference `PolicyToolMiddleware` or
  `agent_core.policy_tool_middleware`.
- `policy_tool_gate.py` is the single module for canonical policy args, pure
  gate logic, and ToolBus pre-hook construction.
- `build_agent()` continues to wire policy enforcement through
  `ToolBusHooks(pre_tool_call=[build_policy_pre_hook(...)])`.
- Policy deny and approval-required errors enter ToolBus post hooks but skip
  transform hooks and result limiting.
- All required verification commands pass.
