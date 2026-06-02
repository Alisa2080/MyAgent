# Cron E2E Acceptance Design

## Context

The cron runtime now has the main production pieces in place:

- `CronService` provides a user-facing automatic scheduling loop and writes
  service status.
- `cron.scheduler.tick()` handles expired lease recovery, stale run recovery,
  queued run promotion, due job claiming, output persistence, and delivery
  maintenance.
- `StateStore` owns persistent jobs, runs, delivery events, and concurrency
  policy state.
- Delivery retry is advanced at the start of every tick, including ticks with no
  due jobs.
- Existing tests cover many units and narrow integrations, but they do not yet
  present a small set of end-to-end acceptance tests that read like production
  runtime guarantees.

This phase adds a stable E2E acceptance suite. It should use the real service,
scheduler, state store, output, and delivery state paths while replacing only
external dependencies with fakes.

## Goals

1. Add a dedicated `tests/test_cron_e2e.py` suite for cron runtime acceptance.
2. Verify the full chain: create job, service tick, run record, output file,
   delivery success, and service status.
3. Verify delivery failure and retry across ticks, including retry on a tick
   with no due jobs.
4. Verify a new `CronService` instance can recover persisted queued, running,
   and stale state after a simulated service restart.
5. Verify `replace_running`, `skip_if_running`, and queue policy behavior through
   the scheduler/service layer, not only through direct `StateStore` calls.

## Non-Goals

- Do not start systemd or launchd.
- Do not run a real LLM, real agent runner, or real subprocess worker.
- Do not send real HTTP requests.
- Do not add slow sleeps or polling loops to default tests.
- Do not replace the existing unit tests for `StateStore`, scheduler, delivery,
  or service platform adapters.

## Test Boundary

The E2E suite should run in process:

- use a temporary `AGENT_CRON_HOME`;
- create jobs through the same public/runtime job APIs used by cron commands or
  the cron job tool where practical;
- run `CronService(once=True)` with a deterministic `tick_fn`;
- use `cron.scheduler.tick(now_text=..., job_runner=fake_runner)` inside that
  `tick_fn`;
- use fake `JobRunResult` values from the job runner;
- use a fake `webhook_sender` installed through the delivery registry path so
  delivery still exercises the real registry, adapter, dispatcher, store, and
  retry state machine.

The suite is end-to-end for the cron runtime boundary. It is not end-to-end for
the operating system service manager, LLM execution, or network transport.

## Test Harness Shape

`tests/test_cron_e2e.py` should define small local helpers:

- a fixture that sets `AGENT_CRON_HOME` to `tmp_path` and resets delivery adapter
  factory state after each test;
- `run_service_once(now_text, job_runner)` that creates a `CronService` with a
  deterministic owner, clock, and `tick_fn` that calls `cron.scheduler.tick`;
- fake runners that record job ids and return deterministic `JobRunResult`
  objects;
- fake webhook senders that return scripted `(status_code, body)` responses;
- helpers to read recent runs, output files, delivery events, and service status.

The helper should keep assertions at the user-visible level:

- run statuses and exit reasons;
- output file existence/content;
- delivery event status and attempt count;
- job `last_status` and `last_delivery_error`;
- service `last_tick` summary.

## Acceptance Scenarios

### 1. Job to Output to Successful Delivery

Create a due job with `deliver="webhook:https://example.invalid/hook"`.

Run one service tick with a fake successful runner and a fake webhook sender that
returns 200.

Assert:

- service exits successfully in `once=True` mode;
- exactly one run exists for the job and is `succeeded`;
- run stores an output path;
- output file exists and contains the fake runner output;
- delivery event exists, is `delivered`, has one attempt, and references the run;
- run delivery status is `delivered`;
- job has no `last_delivery_error`;
- service status has `last_tick.ran == 1` and delivery delivered count is one.

### 2. Delivery Failure Then Retry Without a Due Job

Create a due webhook job. The fake runner succeeds. The fake webhook sender
returns 500 on first call and 200 on second call.

Run the first service tick at the due time.

Assert:

- job execution succeeded and output was saved;
- delivery event is `failed` or retrying according to the existing delivery
  state model;
- run delivery status records retry/failure state;
- job `last_delivery_error` includes the webhook failure.

Then make the job not due anymore and run a second service tick with no due jobs.

Assert:

- the fake runner was not called on the second tick;
- delivery retry was still claimed and delivered;
- service `last_tick.due == 0`;
- service `last_tick.delivery.claimed >= 1`;
- delivery event is `delivered`;
- run delivery status is `delivered`;
- job delivery error is cleared or no longer reports the retried failure,
  matching current dispatcher aggregation behavior.

### 3. Restart Recovery for Queued and Stale Runs

Use one `AGENT_CRON_HOME` and create persisted state:

- a running run with an expired lease or stale heartbeat;
- a queued run that is eligible to promote after the stale run is abandoned.

Construct a fresh `CronService` instance to simulate service restart and run one
tick.

Assert:

- the old stale/running run is completed as failed/abandoned with the expected
  stale/timeout exit reason;
- the queued run is promoted to claimed/running and then completed by the fake
  runner;
- output is saved for the promoted run;
- service status is written by the new service owner;
- recovery does not depend on in-memory objects from the first service instance.

This scenario should avoid OS process restart. The restart boundary is a new
service object reading the same SQLite state.

### 4. Concurrency Policy Integration

Exercise policies through `CronService` and `cron.scheduler.tick`, not by calling
`StateStore.claim_due_jobs()` as the assertion target.

Required scenarios:

- `skip_if_running`: with an existing active run for the same concurrency key,
  a due job creates a skipped run with `exit_reason == "concurrency_skip"` and
  the fake runner is not called for that skipped job.
- `replace_running`: a due job for the same key abandons/replaces the old active
  run, records `replaced_by_run_id`, and executes the newer run through the fake
  runner.
- queue policy, preferably `queue_all`: a second due job queues behind an active
  run, then a later service tick promotes and executes the queued run after the
  key is free.

The tests should assert both persistent run state and fake runner call order.

## Delivery Adapter Strategy

The E2E tests should use webhook delivery because it is the supported active
dispatch adapter. Network I/O must remain fake:

- register or inject a fake webhook sender through the same registry path used
  by existing delivery tests;
- return scripted responses to model success, retryable failure, and recovery;
- assert payloads contain the expected job/run context only when that is stable
  in the existing adapter contract.

Do not start a local HTTP server in this phase.

## Time and Determinism

All tests should pass explicit ISO timestamps to the service tick wrapper. Avoid
depending on wall-clock time except where the store requires "stale" ages; for
those cases write the persisted timestamps directly or use existing store APIs
that accept `now_text`.

No test should sleep.

## Expected Code Changes

The ideal implementation is mostly tests. Runtime code should only change if the
E2E tests expose a real integration gap. If runtime changes are needed, they
should stay narrowly scoped and preserve existing public behavior.

Likely files:

- create `tests/test_cron_e2e.py`;
- possibly add small test-only helper patterns local to that file;
- no production helper module unless repeated setup becomes hard to read.

## Verification

Focused verification should include:

```bash
python -m pytest tests/test_cron_e2e.py -q
python -m pytest tests/test_cron_scheduler.py tests/test_cron_service.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py -q
```

Before completion, run the broader cron-focused suite that has been used in
recent phases:

```bash
python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py tests/test_cron_e2e.py -q
```

## Acceptance Criteria

1. `tests/test_cron_e2e.py` exists and passes without real OS services, LLM
   calls, subprocess runner jobs, or network I/O.
2. The suite proves job creation through service execution produces run records,
   output files, delivery events, and service status.
3. The suite proves delivery retry advances on a no-due-job tick.
4. The suite proves a fresh service instance recovers persisted queued/running
   stale state.
5. The suite proves `replace_running`, `skip_if_running`, and queue behavior at
   the scheduler/service integration layer.
