# ToolBus Phase 4-7 Design

## Goal

Extend the existing lightweight tool catalog and ToolBus design with LangChain-native argument normalization, hook protocol hardening, read-only loop governance, and migration tests. The design keeps LangChain tools as the execution primitive and ports only the useful Hermes dispatch-layer ideas.

This phase does not introduce a Hermes-style registry, agent-level private tool interception, or a replacement for LangChain middleware.

## Existing Context

The project already has:

- `agent_core/tool_catalog.py` with `ToolSpec`, toolset metadata, `read_only`, `risk_level`, `max_result_size_chars`, `enabled_by_default`, and check-function caching.
- `agent_core/builders.py` consuming `build_tools(...)` and passing `tool_specs` into `ToolBusMiddleware`.
- `agent_core/tool_bus_middleware.py` wrapping LangChain tool calls, preserving `ToolMessage` and LangGraph `Command` semantics, running pre/post/transform hooks, normalizing handler exceptions, and truncating oversized results.
- `agent_core/tool_limits.py` using LangChain `ToolCallLimitMiddleware` for specific expensive tools.
- `agent_core/policy_tool_middleware.py` handling policy review separately from ToolBus.

The next phases should refine these boundaries rather than replace them.

## Non-Goals

- Do not copy Hermes `registry.dispatch()` or tool self-registration.
- Do not add agent-level tool interception for private state tools.
- Do not merge `PolicyToolMiddleware` into ToolBus in this phase.
- Do not add file-region deduplication or per-file read tracking. This phase only handles generic consecutive read-only calls.
- Do not rename existing tools or change public tool schemas except for conservative argument normalization before invocation.

## Phase 4: Argument Normalization and Schema Validation

Add `agent_core/tool_arg_coercion.py`.

The module owns argument normalization and validation. `ToolBusMiddleware` delegates to it before running pre-hooks and before calling the handler.

### Interface

The implementation should expose a small interface:

```python
def normalize_tool_args(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    ...
```

If validation fails, the module should raise a controlled project exception, for example:

```python
class ToolArgCoercionError(ValueError):
    tool_name: str
    field_errors: list[str]
```

`ToolBusMiddleware` catches this exception and returns:

```python
tool_failure(tool_name, message, code="invalid_input", runtime=...)
```

The handler must not be called on invalid input.

### Schema Priority

Normalization should use the richest available schema source:

1. `tool.args_schema` using Pydantic v2 validation when available.
2. `tool.args_schema.model_json_schema()` or `model_fields` when direct validation is not appropriate.
3. LangChain tool `.args` JSON schema.
4. No schema means no normalization.

The normalized args must be a new dict. The original `request.tool_call["args"]` must not be mutated in place.

### Conservative Coercions

Allowed coercions:

- `"42"` to `42` when the expected type is integer and the value has no fractional part.
- `"3.14"` to `3.14` when the expected type is number.
- `"true"` and `"false"` to booleans when the expected type is boolean.
- JSON string arrays or objects to `list` or `dict` when the expected type is array/list or object/dict.
- A scalar to a single-item list only when the expected type is explicitly array/list.
- `"null"` to `None` only when the schema explicitly allows null.

Disallowed coercions:

- No date, path, enum, or domain-specific guessing.
- No lossy conversion from decimal strings to integer.
- No conversion of `NaN`, `Infinity`, or `-Infinity` to numeric values.
- No coercion when union alternatives are ambiguous and a safe target cannot be inferred.

### ToolBus Integration

`ToolBusMiddleware._prepare_request()` should:

1. Read the original tool call.
2. Normalize args through `tool_arg_coercion`.
3. If args changed, create an overridden `ToolCallRequest` through the existing request override path.
4. Build `ToolBusRequest` from the call request that will actually be passed to the handler.
5. Preserve `tool_call_id` from `request.tool_call["id"]` or runtime fallback.

Invalid input is a user/model input problem, not a handler exception. It should produce `code="invalid_input"`, not `code="tool_exception"`.

## Phase 5: Plugin Hook Protocol

Keep `ToolBusHooks` as the project-level plugin protocol.

### Hook Signatures

```python
pre_tool_call(request: ToolBusRequest) -> ToolMessage | None
post_tool_call(request: ToolBusRequest, result: ToolBusResult) -> None
transform_tool_result(request: ToolBusRequest, result: ToolMessage) -> ToolMessage | None
```

`ToolBusResult` should continue to include:

- `result`
- `duration_ms`
- `error`

This is more useful than a bare result because observers can distinguish successful tool messages from normalized handler failures.

### Execution Semantics

ToolBus execution order:

1. Prepare request.
2. Normalize and validate args.
3. Run pre-hooks.
4. Call handler.
5. Normalize handler exceptions to `ToolMessage(status="error")`.
6. Run post-hooks.
7. Run transform hooks.
8. Apply result size limits.
9. Return the final result.

Pre-hooks:

- Run in order.
- First `ToolMessage` return blocks handler execution.
- Hook exceptions are logged and ignored.

Post-hooks:

- Observational only.
- Run after handler success and after handler exception normalization.
- Hook exceptions are logged and ignored.

Transform hooks:

- Run only for `ToolMessage` results.
- First `ToolMessage` return wins.
- Hook exceptions are logged and ignored.
- LangGraph `Command` results are not transformed or truncated.

## Phase 6: Consecutive Read-Only Tool Governance

Add a dedicated middleware, for example `ConsecutiveReadOnlyToolLimitMiddleware`.

This middleware handles generic read-loop pressure and stays separate from ToolBus because it is call governance, not dispatch normalization.

### Inputs

```python
ConsecutiveReadOnlyToolLimitMiddleware(
    specs: Mapping[str, ToolSpec],
    max_consecutive_read_only: int = 10,
    include_unknown_as_read_only: bool = False,
)
```

### Behavior

- If the current tool has `spec.read_only is True`, increment the consecutive read-only counter.
- If the current tool is known and not read-only, reset the counter.
- If the current tool is unknown, default to non-read-only unless `include_unknown_as_read_only=True`.
- If the read-only counter exceeds the configured threshold, block the tool call with `tool_failure(..., code="read_loop_limit")`.
- The failure message should tell the agent to stop repeated reading, summarize existing context, and switch strategy or take a non-read action.

### State

Use LangChain middleware state, not module-global state. The initial implementation only needs run-level semantics. Thread-level persistence can be added later if the project needs it.

### Middleware Order

Recommended order in `build_agent`:

```python
middleware=[
    SummarizationMiddleware(...),
    TodoListMiddleware(...),
    *build_tool_call_limit_middleware(include_task=True),
    ConsecutiveReadOnlyToolLimitMiddleware(specs=tool_specs),
    FlexibleHumanInTheLoopMiddleware(...),
    ToolBusMiddleware(specs=tool_specs),
    PolicyToolMiddleware(...),
    ToolRetryMiddleware(...),
    ModelRetryMiddleware(...),
]
```

This preserves the current policy boundary while adding generic read-loop protection before approval and execution middleware.

## Phase 7: Testing and Migration Acceptance

### New Tests

Add:

- `tests/test_tool_arg_coercion.py`
- `tests/test_read_only_tool_limit_middleware.py`

Extend:

- `tests/test_tool_bus_middleware.py`
- `tests/test_agent_cli_builders.py`

### Acceptance Cases

Argument normalization:

- String integers, numbers, booleans, arrays, and objects normalize when schema explicitly asks for them.
- Scalar to list conversion works only for list targets.
- Null conversion works only when null is allowed.
- Ambiguous or unsafe values remain unchanged or fail validation.
- Pydantic validation failure returns `ToolMessage(status="error")` with `code="invalid_input"`.
- The handler is not called for invalid input.
- The original request args are not mutated.

ToolBus hooks:

- Pre-hook blocking does not call the handler.
- Handler exceptions become `ToolMessage(status="error")`.
- Post-hook receives both successful results and normalized handler failures with duration.
- Transform hook can replace `content` and `artifact`.
- Transform is followed by result size limiting.
- `runtime.tool_call_id` remains available when the original runtime does not expose it.
- `Command` results are not transformed or truncated.

Read-only governance:

- Consecutive read-only calls increment the counter.
- Non-read-only calls reset the counter.
- Exceeding the threshold returns `code="read_loop_limit"`.
- Unknown tools default to non-read-only.
- The middleware uses `ToolSpec.read_only` rather than hard-coded tool names.

Builder migration:

- `build_agent(include_cron_tools=True)` still includes cron tools.
- `build_agent(include_cron_tools=False)` still excludes cron tools.
- `ToolBusMiddleware` receives the same `tool_specs` used by read-only governance.
- Middleware order is locked by tests: tool-specific limits, read-only loop middleware, human loop, ToolBus, Policy, retries.

## Migration Strategy

1. Extract argument normalization from `tool_bus_middleware.py` into `tool_arg_coercion.py` without changing behavior.
2. Add validation failure handling and `invalid_input` failures.
3. Harden hook protocol tests around current `ToolBusHooks`.
4. Add `ConsecutiveReadOnlyToolLimitMiddleware` using `ToolSpec.read_only`.
5. Wire the new middleware into `build_agent`.
6. Run focused tests for ToolBus, catalog, builders, and limits.

## Open Decisions

- Default read-only threshold is `10`. Lower values may block legitimate exploration; higher values reduce protection value.
- Policy remains separate from ToolBus for now. A later design can migrate policy checks into a pre-hook once behavior is stable.
- Thread-level read-only counters are deferred until there is a demonstrated need.
