# RuntimeContext Design

## Goal

Introduce a small runtime identity abstraction that centralizes how the project
derives LangGraph thread identity, Hermes task identity, and LangChain tool call
identity.

The design should prepare the codebase for a future policy middleware, but the
first implementation should only replace existing duplicated identity extraction
in tool wrappers.

## Current Context

The project already derives Hermes task ids from LangGraph thread ids in
`agent_core.session_context`. The current rules are correct and must remain
stable:

- `runtime.execution_info.thread_id` is preferred.
- `runtime.config["configurable"]["thread_id"]` is the fallback.
- Missing or empty thread ids use the Hermes `"default"` task id.
- Non-empty thread ids are hashed into a path-safe id using the existing
  `lg_` prefix plus the first 24 hex chars of a sha256 digest.

Duplicated runtime identity extraction currently appears in the public file,
terminal, process, and cron tool wrappers. These wrappers separately extract
`task_id`, `tool_call_id`, or `thread_id` before passing values to policy,
approval, Hermes, and cron origin logic.

## Recommended Approach

Use a lightweight frozen dataclass that describes runtime identity, without
coupling it to policy evaluation or approval storage.

```python
@dataclass(frozen=True)
class RuntimeContext:
    thread_id: str | None
    task_id: str
    tool_call_id: str | None
    thread_source: Literal["execution_info", "config", "fallback"]

    @classmethod
    def from_runtime(cls, runtime: Any | None) -> "RuntimeContext": ...

    @classmethod
    def from_thread_id(cls, thread_id: str | None) -> "RuntimeContext": ...

    @property
    def has_thread(self) -> bool: ...

    @property
    def is_default_task(self) -> bool: ...
```

This keeps the abstraction focused on identity. A later `PolicyToolMiddleware`
can accept a `RuntimeContext`, but this class should not import or call the
permissions layer.

## Scope

First implementation should update these call sites:

- `agent_tools/public/files.py`
  - Replace local task id and tool call id helper usage with `RuntimeContext`.
  - Keep file policy, approval, path checking, and result wrapping unchanged.
- `agent_tools/public/terminal.py`
  - Replace repeated task id and tool call id extraction with
    `RuntimeContext.from_runtime(runtime)`.
  - Keep terminal/process policy decisions and Hermes calls unchanged.
- `agent_tools/public/cronjob.py`
  - Replace `_runtime_thread_id(runtime)` with `RuntimeContext.from_runtime(runtime).thread_id`.

Optional, low-priority follow-up:

- `agent_core/agent_runner.py`
  - It currently parses invoke config directly rather than receiving
    `ToolRuntime`. Leave it unchanged in the first implementation unless a
    separate `RuntimeContext.from_config(config)` is introduced with tests.

## Non-Goals

This change must not:

- Change task id hashing or the `"default"` fallback behavior.
- Expose `runtime`, `thread_id`, `task_id`, or `tool_call_id` in any tool schema.
- Change approval, audit, or policy decision semantics.
- Rewrite `FlexibleHumanInTheLoopMiddleware`.
- Introduce a policy-aware runtime context object.
- Change Hermes terminal, process registry, file backend, cron job, or cleanup
  behavior.

## Data Flow

Tool wrappers should use a single local context object:

```python
ctx = RuntimeContext.from_runtime(runtime)
```

Then:

- Use `ctx.task_id` for Hermes task isolation and backend path policy.
- Use `ctx.tool_call_id` for approval consumption.
- Use `ctx.thread_id` for cron origin metadata.
- Use `ctx.thread_source` only for diagnostics or future middleware decisions.

The parsing order is:

1. Use `runtime.execution_info.thread_id` when present and truthy.
2. Otherwise use `runtime.config["configurable"]["thread_id"]` when present and
   truthy.
3. Otherwise set `thread_id=None`, `thread_source="fallback"`, and
   `task_id="default"`.

`tool_call_id` is extracted independently from `runtime.tool_call_id` when
present and truthy. It does not affect task id derivation.

## Compatibility

The existing functions remain public and keep their behavior:

```python
hermes_task_id_from_thread_id(thread_id)
hermes_task_id_from_runtime(runtime)
```

They can delegate to `RuntimeContext`, but their observable outputs must stay
the same. Existing callers in terminal lifecycle and terminal notification code
do not need to migrate in the first implementation.

## Error Handling

Runtime identity extraction should be lenient:

- `runtime=None` is valid and falls back to `"default"`.
- Missing `execution_info` is valid.
- Non-dict `config` is ignored.
- Missing or non-dict `configurable` is ignored.
- Empty string thread ids are treated as missing.
- Missing `tool_call_id` returns `None`.

The abstraction should not raise new user-facing errors. Existing policy and
approval layers remain responsible for rejecting reviewed tool calls when a
`tool_call_id` is required but absent.

## Testing

Extend `tests/test_session_context.py` to cover:

- `RuntimeContext.from_runtime()` prefers `execution_info.thread_id`.
- It falls back to `config.configurable.thread_id`.
- Missing runtime creates `thread_id=None`, `task_id="default"`, and
  `thread_source="fallback"`.
- Empty thread ids use the `"default"` task id.
- `tool_call_id` is extracted when present.
- `has_thread` and `is_default_task` reflect the resolved identity.
- `hermes_task_id_from_runtime()` and `hermes_task_id_from_thread_id()` remain
  backward compatible.

Existing file and terminal wrapper tests should continue to verify that:

- Tool schemas do not expose runtime identity fields.
- ToolNode config injection still maps to the expected Hermes task id.
- Reviewed tool calls without `tool_call_id` retain current approval behavior.

## Rollout

Implement as a small refactor:

1. Add `RuntimeContext` and tests in `agent_core.session_context`.
2. Migrate `agent_tools/public/terminal.py`.
3. Migrate `agent_tools/public/files.py`.
4. Migrate `agent_tools/public/cronjob.py`.
5. Run focused session-context, terminal, file-runtime, file-wrapper,
   terminal-wrapper, and cronjob tests.

This leaves the codebase ready for a later policy middleware without changing
the current permission model in the same patch.
