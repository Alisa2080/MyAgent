# Cron Runner Dependency Injection Design

## Summary

Refactor the cron scheduler/runner boundary so scheduling remains lightweight and import-safe while cron job execution can still use the full LangChain agent stack when a job actually runs.

The selected approach is a medium refactor:

- Move cron execution contracts into a lightweight module.
- Keep `cron.scheduler.tick(now_dt=None)` fully backward compatible.
- Add a keyword-only `job_runner` injection point for tests and future runner implementations.
- Keep the real runner lazily loaded so importing `cron.scheduler` does not import `cron.runner` or LangChain agent dependencies.

## Problem

`cron.runner` imports heavy agent dependencies such as `langchain.agents.create_agent` and several `agent_core` modules. That is appropriate for the implementation that actually invokes a cron agent, but it is not appropriate for the scheduler import path.

The scheduler should be a lightweight coordinator:

- find due jobs
- advance schedules
- call a runner
- save output
- enqueue delivery
- mark run state

It should not need to import the concrete agent runner at module import time.

The current branch uses a pragmatic lazy wrapper in `cron.scheduler.run_job()` and a duplicate `JobRunResult` in `cron.delivery`. This works, but the contract boundary is unclear:

- `JobRunResult` exists in more than one place.
- `delivery.py` owns a runner result type even though delivery is not the runner layer.
- tests rely on monkeypatching `scheduler.run_job` instead of passing the dependency explicitly.
- future runner implementations would have to copy the implicit callable shape.

## Goals

1. Keep `import cron.scheduler` lightweight and safe in tests, CLI startup, and gateway startup.
2. Define cron runner contracts exactly once in a lightweight module.
3. Preserve public compatibility for existing scheduler callers.
4. Make scheduler tests explicit by allowing a fake runner to be injected.
5. Keep production behavior unchanged when no runner is injected.
6. Make future runner strategies easier to add, such as subprocess or remote execution.

## Non-Goals

- Do not move cron execution into a subprocess in this change.
- Do not redesign job storage, delivery queues, or cron schedules.
- Do not change cron job output format or delivery payload shape.
- Do not change CLI commands or gateway lifecycle behavior beyond using the same compatible scheduler API.
- Do not remove the real `cron.runner.run_job()` implementation.

## Proposed Architecture

### `cron/contracts.py`

Add a new lightweight module with no LangChain, agent, tool, or CLI imports.

It owns:

- `JobRunResult`
- `JobRunner`

Proposed shape:

```python
from __future__ import annotations

from collections.abc import Protocol
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobRunResult:
    success: bool
    output_doc: str | None = None
    final_response: str | None = None
    error: str | None = None


class JobRunner(Protocol):
    def __call__(self, job: dict[str, Any]) -> JobRunResult:
        ...
```

`output_doc` and `final_response` remain optional to match current test and delivery usage. The concrete runner can still populate them with strings.

### `cron.runner`

`cron.runner` remains the heavy implementation module. It imports `JobRunResult` from `cron.contracts` and continues to expose:

```python
def run_job(job: dict[str, Any]) -> JobRunResult:
    ...
```

This module may continue importing LangChain and `agent_core` dependencies because it is only loaded when the default runner is actually used.

### `cron.scheduler`

`cron.scheduler` imports only lightweight contracts at module import time:

```python
from cron.contracts import JobRunResult, JobRunner
```

It provides a private lazy loader:

```python
def _default_job_runner() -> JobRunner:
    from cron.runner import run_job

    return run_job
```

`tick()` stays compatible and adds a keyword-only injection point:

```python
def tick(now_dt: datetime | None = None, *, job_runner: JobRunner | None = None) -> TickResult:
    ...
```

Internal helpers receive the resolved runner:

```python
def _process_job(job: dict[str, Any], run_at: datetime, job_runner: JobRunner) -> JobTickResult:
    result = job_runner(advanced)
```

`_run_parallel()` also accepts the runner and submits it through `_process_job()`.

For backward compatibility, `tick()` resolves the default only after due jobs are known and a job is about to run. If no jobs are due, the heavy runner should not be imported.

### `cron.delivery`

`cron.delivery` imports `JobRunResult` from `cron.contracts`.

It must not define runner result types. Delivery remains responsible for:

- parsing delivery targets
- validating webhook URLs
- building delivery payloads
- enqueueing and processing delivery events

### Tests

Tests should prefer explicit runner injection:

```python
result = scheduler.tick(
    now_dt=RUN_AT,
    job_runner=lambda job: JobRunResult(True, "doc", "final", None),
)
```

Monkeypatching `scheduler.run_job` should no longer be necessary for new tests. Existing tests can be migrated as part of this refactor.

Add an import-health regression test proving:

- importing `cron.scheduler` does not import `cron.runner`
- importing `cron.scheduler` does not import `langchain.agents`
- `tick()` with no due jobs does not import the heavy runner
- `tick()` with an injected fake runner does not import the heavy runner

## Data Flow

Default production flow:

1. `agent_core.cron_lifecycle.tick()` calls `cron.scheduler.tick()`.
2. `scheduler.tick()` gets due jobs.
3. If jobs are due and no runner was injected, scheduler lazily resolves `cron.runner.run_job`.
4. Scheduler advances each job schedule.
5. Scheduler calls the runner and receives `JobRunResult`.
6. Scheduler saves output, enqueues delivery, processes due local/webhook delivery, and marks job state.

Injected test flow:

1. Test calls `cron.scheduler.tick(job_runner=fake_runner)`.
2. Scheduler never imports `cron.runner`.
3. Fake runner returns `JobRunResult`.
4. Scheduler behavior is tested through real scheduling, output, delivery, and mark-run logic.

## Error Handling

Runner behavior remains unchanged:

- A runner can return `JobRunResult(success=False, error=...)` for handled job failures.
- A runner can raise an exception for unexpected failures.

Scheduler behavior remains unchanged:

- Handled failures still save output, enqueue delivery, and mark job run state.
- Unexpected runner exceptions are caught by `_process_job()`, logged, and recorded through `mark_job_run(success=False, error=...)`.

The default runner lazy import should not catch import errors silently. If production cannot import the real runner when a due job needs execution, the job should fail visibly through existing scheduler error handling.

## Compatibility

Existing public calls continue to work:

```python
tick()
tick(now_dt=some_datetime)
```

The new parameter is keyword-only, so positional callers are unaffected.

Existing gateway/lifecycle code does not need to change immediately.

The old `scheduler.run_job()` wrapper should be removed or kept only if existing code outside tests uses it. A repository search should determine this during implementation. If kept temporarily, it should delegate to `_default_job_runner()` and be marked as compatibility-only.

## Acceptance Criteria

1. `JobRunResult` is defined in exactly one production module: `cron.contracts`.
2. `cron.runner` imports `JobRunResult` from `cron.contracts`.
3. `cron.delivery` imports `JobRunResult` from `cron.contracts`.
4. `cron.scheduler` imports no heavy agent modules and does not top-level import `cron.runner`.
5. `tick(now_dt=None)` remains compatible.
6. `tick(now_dt=None, *, job_runner=None)` supports fake runner injection.
7. Existing cron scheduler, delivery, CLI, and notification tests pass.
8. New tests verify import health and runner injection behavior.

## Risks

- Moving `JobRunResult` can break tests or modules that import it from `cron.delivery` or `cron.runner`.
- Resolving the default runner too early would preserve the current heavy import problem.
- Parallel execution needs runner propagation into thread-pool jobs.
- Keeping multiple compatibility import paths for `JobRunResult` can obscure the single-source-of-truth goal.

## Mitigations

- Update production imports first, then migrate tests.
- Add import-health tests before or during implementation.
- Resolve the default runner only after due jobs are known.
- If compatibility aliases are needed, keep them short-lived and covered by a follow-up cleanup note.

## Open Questions

None. The selected design is medium refactor with full `tick()` compatibility and keyword-only runner injection.
