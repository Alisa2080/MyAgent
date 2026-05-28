# Cron Runner Subprocess Boundary Design

## Summary

Add an optional subprocess boundary for cron job execution so `cron.scheduler` remains responsible for scheduling and state, while heavy runner dependencies and runner crashes are isolated from the scheduler process.

The selected approach is a staged implementation:

- Keep the current in-process runner as the default.
- Add `AGENT_CRON_RUNNER_MODE=subprocess` to opt into per-job subprocess execution.
- Use temporary input/result files for structured communication.
- Keep stdout/stderr as diagnostic logs, not protocol data.
- Use dual-layer timeouts: existing runner timeouts inside the child process plus a parent-process kill timeout.
- Design the client/worker protocol so a later long-lived worker can reuse the same result model.

## Problem

The previous dependency-injection refactor made `cron.scheduler` import-safe and lightweight. It no longer imports `cron.runner` or LangChain-heavy dependencies at module import time.

However, the default execution path still invokes the real runner inside the scheduler process. This means a severe runner-side problem can still affect the scheduler process:

- heavy dependency import crashes
- native/runtime crashes
- memory leaks or state pollution
- stuck execution beyond the runner's internal safeguards
- unexpected process-level side effects from agent/tool code

The scheduler should be able to continue ticking and marking job state even when the runner implementation fails badly.

## Goals

1. Isolate cron job execution from the scheduler process when subprocess mode is enabled.
2. Preserve current default behavior during the first rollout by keeping in-process mode as default.
3. Preserve the `JobRunner` contract: callers receive a `JobRunResult`.
4. Keep `cron.scheduler` focused on scheduling, output persistence, delivery enqueueing, and job state.
5. Avoid using stdout as the structured protocol because runner/agent dependencies may write logs.
6. Provide clear failure mapping for child import errors, crashes, missing results, invalid results, and parent timeout.
7. Expose runner mode and subprocess health through status/doctor diagnostics.
8. Leave room for a future long-lived worker without implementing it now.

## Non-Goals

- Do not make subprocess mode the default in this change.
- Do not implement a persistent worker daemon yet.
- Do not redesign cron scheduling, job persistence, delivery queues, or output storage.
- Do not change cron job prompt construction or agent behavior.
- Do not require scheduler callers to know whether execution is in-process or subprocess.

## Proposed Architecture

### Existing Contract

`cron.contracts.JobRunResult` remains the common result type:

```python
@dataclass(frozen=True)
class JobRunResult:
    success: bool
    output_doc: str | None = None
    final_response: str | None = None
    error: str | None = None
```

`cron.scheduler` continues to call a `JobRunner` callable and handles `JobRunResult` exactly as it does today.

### New `cron.runner_client`

Add a lightweight default runner selector:

```python
def run_job(job: dict[str, Any]) -> JobRunResult:
    mode = runner_mode()
    if mode == "subprocess":
        from cron.runner_subprocess import run_job_subprocess
        return run_job_subprocess(job)
    if mode == "inprocess":
        from cron.runner import run_job as run_job_inprocess
        return run_job_inprocess(job)
    return JobRunResult(...)
```

Responsibilities:

- read `AGENT_CRON_RUNNER_MODE`
- normalize mode names
- choose in-process or subprocess implementation
- produce a handled failure result for unsupported modes

`cron.scheduler._run_default_job()` should lazy-import `cron.runner_client.run_job`, not `cron.runner.run_job`.

### New `cron.runner_subprocess`

Add the parent-process subprocess client.

Responsibilities:

- create a temporary run directory under cron home
- write the job payload to `input.json`
- invoke the worker entrypoint with the current Python executable:

```bash
python -m cron.runner_worker --input <input.json> --output <result.json>
```

- enforce parent timeout from `AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT`, default `900` seconds
- terminate on timeout, then kill after a short grace period
- read and validate `result.json`
- capture bounded stdout/stderr diagnostics
- convert every failure mode into `JobRunResult`

The parent should not parse stdout as JSON. stdout and stderr are diagnostic text only.

### New `cron.runner_worker`

Add the child-process entrypoint.

Responsibilities:

- parse `--input` and `--output`
- load job JSON from input file
- call `cron.runner.run_job(job)`
- serialize the returned `JobRunResult` to output file
- on handled Python exceptions, write a failure `JobRunResult`
- return process exit code `0` when a result file was written, even if the job result is `success=False`
- return nonzero only for worker-level failures that prevent writing a result

The worker may import heavy dependencies because it runs outside the scheduler process.

### Result File Protocol

Result JSON shape:

```json
{
  "version": 1,
  "success": true,
  "output_doc": "...",
  "final_response": "...",
  "error": null
}
```

Rules:

- `version` must be `1`.
- unknown fields are ignored by the parent.
- missing required fields make the parent return a failure `JobRunResult`.
- `output_doc`, `final_response`, and `error` are nullable strings.
- parent-side failure results should include bounded diagnostics when useful.

### Temporary Files

Use a per-run temp directory under cron home, for example:

```text
<cron_home>/runner-tmp/<event-id-or-random>/
  input.json
  result.json
  stdout.log
  stderr.log
```

The parent may delete temp directories after successful parse. For failures, it should either:

- keep the directory and reference it in diagnostics, or
- include bounded stdout/stderr in the failure output and remove the directory.

The first implementation should prefer cleanup after capturing bounded diagnostics to avoid unbounded disk growth. If retention is needed later, add a retention policy and doctor cleanup check.

## Data Flow

### In-Process Mode

1. Scheduler calls `_run_default_job(job)`.
2. `_run_default_job()` imports `cron.runner_client.run_job`.
3. `runner_client` sees `AGENT_CRON_RUNNER_MODE` unset or `inprocess`.
4. `runner_client` lazy-imports `cron.runner.run_job`.
5. Scheduler receives `JobRunResult` and continues existing output/delivery/state flow.

### Subprocess Mode

1. Scheduler calls `_run_default_job(job)`.
2. `_run_default_job()` imports `cron.runner_client.run_job`.
3. `runner_client` sees `AGENT_CRON_RUNNER_MODE=subprocess`.
4. `runner_subprocess` writes `input.json`.
5. Parent starts `python -m cron.runner_worker`.
6. Worker loads input, imports `cron.runner`, executes job, writes `result.json`.
7. Parent parses `result.json` into `JobRunResult`.
8. Scheduler saves output, enqueues delivery, processes delivery, and marks job state.

## Failure Mapping

All subprocess client failures must return `JobRunResult(success=False, ...)` instead of raising to scheduler unless the parent process itself is interrupted.

| Failure | Result |
| --- | --- |
| unsupported `AGENT_CRON_RUNNER_MODE` | failure result with clear configuration error |
| worker import failure | failure result from worker if result file written; otherwise parent maps nonzero exit |
| worker raises handled exception | worker writes failure result |
| worker exits nonzero without result | parent returns failure result including return code and bounded stderr |
| result file missing | parent returns failure result |
| result JSON invalid | parent returns failure result including parse error |
| result schema invalid | parent returns failure result |
| parent timeout | parent terminates/kills child and returns timeout failure result |
| parent interrupted | allow interruption to propagate when appropriate |

Parent timeout error text should be explicit:

```text
Cron runner subprocess timed out after <N> seconds.
```

## Timeout Design

Use dual-layer timeouts:

- Existing runner internals keep their current script timeout and agent execution safeguards.
- Parent process enforces a final subprocess timeout with `AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT`.

Default parent timeout: `900` seconds.

On timeout:

1. send terminate
2. wait `AGENT_CRON_RUNNER_TERMINATE_GRACE_SECONDS`, default `5`
3. kill if still alive
4. return failure `JobRunResult`

This prevents scheduler threads from waiting forever even if child code wedges below Python-level timeouts.

## Diagnostics

### Status

`cron status` should include:

- runner mode: `inprocess` or `subprocess`
- subprocess timeout when subprocess mode is active

### Doctor

`cron doctor` should check:

- runner mode value is supported
- worker entrypoint is resolvable in subprocess mode
- subprocess timeout env var is a positive integer
- temp directory parent is writable

Doctor should not run an actual cron job by default. A future explicit `cron test-runner` command can perform an end-to-end worker smoke test if needed.

## Compatibility

The public scheduler API remains unchanged:

```python
tick()
tick(now_dt=...)
tick(now_dt=..., job_runner=...)
```

No caller has to pass runner mode. The default runner selection is internal to `cron.runner_client`.

Default behavior remains in-process until a later rollout explicitly changes the default.

## Testing Strategy

### Unit Tests

- `runner_client` selects in-process mode by default.
- `runner_client` selects subprocess mode when configured.
- unsupported runner mode returns failure result.
- subprocess timeout value parsing handles invalid values.
- result JSON serialization/deserialization round trips.

### Subprocess Client Tests

- worker success result is parsed.
- worker handled failure result is parsed.
- nonzero worker exit without result maps to failure.
- missing result file maps to failure.
- invalid result JSON maps to failure.
- invalid schema maps to failure.
- parent timeout terminates/kills child and returns failure.

### Worker Tests

- worker reads input and writes result.
- worker maps Python exception to failure result.
- worker exits nonzero when input cannot be read or output cannot be written.

### Scheduler Integration Tests

- scheduler still does not import `cron.runner` at module import time.
- scheduler in default in-process mode preserves existing behavior.
- scheduler with `AGENT_CRON_RUNNER_MODE=subprocess` receives a `JobRunResult` through the client.
- runner subprocess failure is marked through existing scheduler `mark_job_run` path.

### Existing Test Suites

Continue running cron-focused suites:

```bash
python -m pytest \
  tests/test_cron_delivery_store.py \
  tests/test_cron_delivery.py \
  tests/test_cron_notifications.py \
  tests/test_cron_scheduler.py \
  tests/test_cron_import_health.py \
  tests/test_agent_cli_cron_commands.py \
  tests/test_agent_cli_main.py \
  tests/test_cronjob_tool.py \
  tests/test_cron_lifecycle.py \
  tests/test_agent_tools_public_imports.py \
  tests/test_cron_runner.py
```

## Rollout Plan

1. Implement `runner_client`, `runner_subprocess`, and `runner_worker`.
2. Keep default `AGENT_CRON_RUNNER_MODE` as `inprocess`.
3. Add status/doctor diagnostics.
4. Add tests for subprocess protocol and failure mapping.
5. Run with `AGENT_CRON_RUNNER_MODE=subprocess` in focused environments.
6. Later, after stability is proven, decide whether to change the default.

## Acceptance Criteria

1. `cron.scheduler` remains import-safe and does not top-level import `cron.runner`.
2. In-process mode remains the default.
3. `AGENT_CRON_RUNNER_MODE=subprocess` runs each cron job in a child process.
4. The parent/child protocol uses temporary input/result files, not stdout JSON.
5. Parent timeout returns a failure `JobRunResult` and does not hang scheduler indefinitely.
6. Worker crashes and invalid/missing result files map to failure `JobRunResult`.
7. Existing cron job success/failure delivery behavior remains unchanged.
8. `cron status` and `cron doctor` expose runner mode diagnostics.
9. Cron-focused tests pass.

## Risks

- Subprocess startup overhead may be noticeable for frequent cron jobs.
- Environment differences between parent and child can surface hidden assumptions.
- Large `output_doc` or `final_response` values can make result files large.
- Temp directory cleanup bugs can leak files.
- Killing a child process may not kill grandchildren spawned by tools or scripts.

## Mitigations

- Keep subprocess mode opt-in for the first rollout.
- Use bounded diagnostics for stdout/stderr.
- Use a temp directory cleanup policy from the start.
- Keep parent timeout conservative and configurable.
- Consider process-group termination in implementation if child tools can spawn descendants.
- Preserve in-process mode as a supported rollback path.

## Future Work

- Long-lived worker process using the same request/result schema.
- Explicit `cron test-runner` smoke command.
- Runner temp retention policy for debugging failed jobs.
- Metrics for subprocess startup time and failure categories.
