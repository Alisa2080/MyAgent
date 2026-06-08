# Tool Catalog and ToolBus Design

Date: 2026-06-08

## Goal

Introduce a lightweight tool metadata catalog and a LangChain-native tool bus
middleware inspired by Hermes' tool dispatch layer, without replacing
LangChain's tool execution path.

The design covers three implementation stages:

1. Define `ToolSpec` metadata in `agent_core/tool_catalog.py`.
2. Replace the hard-coded `build_agent()` tool list with a dynamic
   `build_tools(...)` helper.
3. Add `ToolBusMiddleware` in `agent_core/tool_bus_middleware.py` for
   pre-call hooks, post-call hooks, result transforms, exception normalization,
   argument coercion, timing, and output limiting.

## Context

The current project already uses LangChain/LangGraph primitives:

- `agent_core/builders.py` constructs the agent with `create_agent(...)`.
- `agent_core/delegation.py` defines `READ_ONLY_TOOLS`, `BASE_TOOLS`, and the
  `task` delegation tool.
- `agent_tools/public/*.py` exposes LangChain tools with `@tool(...,
  args_schema=...)` and `ToolRuntime`.
- `agent_tools/shared/tool_result.py` provides the canonical `ToolMessage`
  success/failure artifact format.
- `agent_core/policy_tool_middleware.py` already gates risky tools such as
  `terminal`, `process`, `write_file`, and `patch`.

Hermes' useful concepts are metadata, toolset filtering, pre/post hooks,
argument coercion, result transformation, and result limiting. Its central
`registry.dispatch()` and JSON-string result protocol should not be copied into
this project because LangChain already owns dispatch and runtime injection.

## Non-Goals

- Do not implement a Hermes-style `registry.dispatch()`.
- Do not replace LangChain `BaseTool` / `@tool` objects.
- Do not change public tool function signatures.
- Do not change the canonical `ToolMessage.artifact` protocol.
- Do not add plugin directory discovery in this phase.
- Do not merge `PolicyToolMiddleware` into `ToolBusMiddleware` in the first
  implementation.
- Do not change `build_agent()`'s public function signature in the first
  implementation.

## Architecture

The design adds two layers around LangChain's native dispatch:

```text
build_agent()
  -> build_tools(...)                  # tool visibility and metadata
  -> create_agent(..., tools=tools)
       -> LangChain tool dispatch
          wrapped by ToolBusMiddleware # call lifecycle and result pipeline
          wrapped by PolicyToolMiddleware
```

`ToolSpec` answers "what is this tool and when should it be visible?"
`ToolBusMiddleware` answers "what should happen around each tool call?"

Tool execution still happens by calling LangChain's `handler(request)`.

## Stage 1: ToolSpec Metadata Layer

Add `agent_core/tool_catalog.py`.

`ToolSpec` should be a frozen dataclass:

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    toolset: str
    tool: Any
    check_fn: Callable[[], bool] | None = None
    max_result_size_chars: int | None = None
    read_only: bool = False
    risk_level: Literal["low", "medium", "high"] = "low"
    emoji: str = ""
    enabled_by_default: bool = True
```

The `tool` field stores the existing LangChain tool object. It is metadata plus
selection, not execution.

Initial toolsets:

| Toolset | Tools |
| --- | --- |
| `file_read` | `list_directory`, `search_files`, `read_file`, `file_info` |
| `file_write` | `write_file`, `patch` |
| `terminal` | `terminal`, `process` |
| `web` | `web_search`, `web_extract` |
| `skills` | `skills_list`, `skill_view`, `skill_manage` |
| `memory` | `memory_manage` |
| `clarify` | `clarify` |
| `delegation` | `task` |
| `cron` | `cronjob` |

Recommended defaults:

- read-only tools: `read_only=True`, `risk_level="low"`.
- `write_file`, `patch`: `read_only=False`, `risk_level="medium"`.
- `terminal`, `process`: `read_only=False`, `risk_level="high"`.
- `skill_manage`, `memory_manage`, `cronjob`: `risk_level="medium"`.
- `clarify`, `task`: `risk_level="low"` unless later policy requires review.

Public helpers:

```python
default_tool_specs(include_cron_tools: bool = False) -> list[ToolSpec]
get_tool_specs(...) -> list[ToolSpec]
get_tool_spec(name: str) -> ToolSpec | None
clear_tool_catalog_cache() -> None
```

`check_fn` should be light and fail closed for tool visibility only. It should
not run slow network probes during agent construction.

## Stage 2: Dynamic Tool List Builder

Add:

```python
build_tools(
    enabled_toolsets: list[str] | None = None,
    *,
    include_cron_tools: bool = False,
    runtime_profile: str | None = None,
) -> list[Any]
```

Behavior:

- If `enabled_toolsets is None`, return the current default set:
  `BASE_TOOLS + memory_manage + task`, plus `cronjob` only when
  `include_cron_tools=True`.
- If `enabled_toolsets` is provided, return tools whose `ToolSpec.toolset`
  appears in that list.
- Preserve the order of the default specs so tool presentation remains stable.
- Apply `enabled_by_default` and `check_fn`.
- Use 30 second TTL caching for `check_fn` results.
- Cache key should include at least `tool_name` and may include
  `runtime_profile`.
- Unknown toolsets should be ignored or logged, not crash agent construction.

`build_agent()` should replace:

```python
tools = [*BASE_TOOLS, memory_manage, task]
```

with:

```python
tools = build_tools(
    enabled_toolsets=None,
    include_cron_tools=include_cron_tools,
    runtime_profile=os.environ.get("AGENT_RUNTIME_PROFILE"),
)
```

First implementation should not expose `enabled_toolsets` through
`build_agent()` yet. That can be a later API change.

Compatibility:

- Keep `BASE_TOOLS` and `READ_ONLY_TOOLS` exports for existing imports and tests.
- The agent should expose the same default tools as before.
- `cronjob` remains opt-in through `include_cron_tools`.

## Stage 3: ToolBusMiddleware

Add `agent_core/tool_bus_middleware.py`.

The middleware should subclass `AgentMiddleware` and implement both sync and
async wrappers:

```python
class ToolBusMiddleware(AgentMiddleware):
    def wrap_tool_call(self, request, handler): ...
    async def awrap_tool_call(self, request, handler): ...
```

Constructor:

```python
class ToolBusMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        hooks: ToolBusHooks | None = None,
        specs: Mapping[str, ToolSpec] | None = None,
        coerce_args: bool = True,
    ) -> None: ...
```

Hook protocol:

```python
PreToolHook = Callable[[ToolBusRequest], ToolMessage | None]
PostToolHook = Callable[[ToolBusRequest, ToolBusResult], None]
TransformToolResultHook = Callable[[ToolBusRequest, ToolMessage], ToolMessage | None]
```

Rules:

- Pre hooks run before `handler(request)`.
- A pre hook returning a `ToolMessage` blocks execution.
- Post hooks observe only and cannot replace the result.
- Transform hooks may replace a `ToolMessage`; the first non-`None` result wins.
- Hook exceptions are logged and ignored.
- Hooks are in-memory for the first implementation. Plugin discovery is out of
  scope.

Execution flow:

1. Read `tool_name`, `args`, and `tool_call_id` from `request.tool_call`.
2. Optionally coerce simple argument types.
3. Run pre hooks.
4. Record `time.monotonic()`.
5. Call `handler(request)`.
6. Convert handler exceptions to `tool_failure(..., code="tool_exception")`.
7. Compute `duration_ms`.
8. Run post hooks.
9. Run transform hooks for `ToolMessage` results.
10. Apply result limits from `ToolSpec.max_result_size_chars`.
11. Return the final `ToolMessage` or `Command`.

`Command` handling:

- If a handler returns `ToolMessage`, apply the full result pipeline.
- If a handler returns `Command`, do not unpack or rewrite graph state in the
  first implementation.
- For `Command`, post hooks may observe the result, but transform and truncation
  should be skipped to avoid corrupting LangGraph state updates.
- Pre hook blocks should return a `ToolMessage`, not a `Command`.

Argument coercion:

- Only operate on dict-like tool args.
- Use available `args_schema` or `tool.args` metadata.
- Supported conversions:
  - `"42"` to `42`
  - `"3.14"` to `3.14`
  - `"true"` / `"false"` to booleans
  - JSON strings to `list` or `dict` when schema expects arrays or objects
- Failed conversions keep the original value.
- Coercion should not bypass Pydantic validation; it is a tolerance layer for
  common model drift.

Result limiting:

- Apply only when `ToolSpec.max_result_size_chars` is set.
- Preserve `artifact.error.code` and `artifact.error.message`.
- Prefer truncating `content` and adding a concise truncation note.
- For large `artifact.data`, preserve the top-level artifact shape and replace
  oversized fields with a truncation summary.
- Avoid mutating the original `ToolMessage` object in place when practical.

Middleware ordering:

```python
middleware=[
    SummarizationMiddleware(...),
    TodoListMiddleware(...),
    *build_tool_call_limit_middleware(include_task=True),
    FlexibleHumanInTheLoopMiddleware(...),
    ToolBusMiddleware(...),
    PolicyToolMiddleware(...),
    ToolRetryMiddleware(...),
    ModelRetryMiddleware(...),
]
```

This keeps existing policy behavior separate. If later tests show policy must
run before generic hooks, ordering can be adjusted without merging the two
middleware classes.

## Hermes Reference Mapping

| Hermes concept | Current project design |
| --- | --- |
| `ToolEntry` | `ToolSpec` metadata around LangChain tool objects |
| `get_tool_definitions()` | `build_tools()` and future toolset APIs |
| `registry.dispatch()` | LangChain tool node dispatch via `handler(request)` |
| `coerce_tool_args()` | ToolBus argument coercion |
| `pre_tool_call` | ToolBus pre hooks |
| `post_tool_call` | ToolBus post hooks |
| `transform_tool_result` | ToolBus transform hooks |
| `max_result_size_chars` | ToolSpec-driven ToolBus result limiting |
| agent-level tools | Existing `ToolRuntime`, middleware, and explicit tools |

## Testing Strategy

Add focused tests without requiring real LangChain packages beyond the existing
test stubs where possible.

Tool catalog tests:

- Default `build_tools()` returns the same tool names as current
  `BASE_TOOLS + memory_manage + task`.
- `include_cron_tools=True` adds `cronjob`.
- `enabled_toolsets=["file_read"]` returns only read file tools.
- `check_fn=False` hides the tool.
- `check_fn` results are cached for the TTL.
- `clear_tool_catalog_cache()` forces re-check.

Builder tests:

- `build_agent(include_cron_tools=False)` passes the same default tools as
  before.
- `build_agent(include_cron_tools=True)` includes `cronjob`.
- Middleware order includes `ToolBusMiddleware` without removing
  `PolicyToolMiddleware`.

ToolBus tests:

- Pre hook returning `ToolMessage` blocks handler execution.
- Handler exception returns `ToolMessage(status="error")` with
  `code="tool_exception"`.
- Post hook receives tool name, args, result, and duration.
- Transform hook replaces the result, and the first valid transform wins.
- Hook exceptions fail open.
- Argument coercion handles integer, number, boolean, list, and dict cases.
- Result limiting truncates content while preserving error metadata.
- `Command` results are not transformed or truncated.
- Async wrapper mirrors sync behavior.

Policy integration tests:

- Existing `PolicyToolMiddleware` tests continue passing.
- ToolBus does not grant approvals or duplicate policy decisions.
- Policy-generated `ToolMessage` errors can still pass through ToolBus when
  middleware ordering causes ToolBus to observe them.

## Acceptance Criteria

- Default agent tool availability is unchanged unless `include_cron_tools=True`.
- `build_agent()` no longer manually concatenates the default tool list.
- Tool metadata is queryable by name.
- Toolset filtering works through `build_tools(...)`.
- Tool calls can be blocked by a ToolBus pre hook.
- Tool results can be observed and transformed by ToolBus hooks.
- Tool exceptions are normalized to `ToolMessage` failures.
- Large tool outputs can be limited per `ToolSpec`.
- Existing policy, terminal, file, memory, clarify, and delegation tests remain
  compatible.

## Rollout Plan

1. Add `tool_catalog.py` and tests while leaving `build_agent()` unchanged.
2. Switch `build_agent()` to `build_tools(...)` and verify default parity.
3. Add `tool_bus_middleware.py` and unit tests with fake requests/handlers.
4. Insert `ToolBusMiddleware` into `build_agent()` middleware order.
5. Add regression tests for policy coexistence and public tool result shape.

Each step should be independently reviewable and should avoid touching unrelated
tool implementation files.
