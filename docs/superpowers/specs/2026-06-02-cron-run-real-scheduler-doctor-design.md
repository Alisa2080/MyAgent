# Cron Run Real Scheduler And Doctor Design

## Context

The cron runtime already has a production scheduler path in
`cron.scheduler.tick()`. A tick recovers expired leases, completes stale runs,
recovers stale delivery events, processes pending or retryable delivery, claims
queued and due jobs, runs them through the configured runner, saves output, and
updates delivery status.

The CLI `agent cron run JOB_ID` still does not use that production path. Its
non-dry-run branch delegates to the legacy simple job action, so a manual run can
bypass the SQLite run state machine, output binding, delivery enqueue/dispatch,
concurrency policy, stale recovery, and the operational behavior users see from
the background service.

Recent Feishu smoke testing also showed that `cron doctor` needs to move from
reporting facts to giving actionable runtime diagnostics. The current doctor
checks many pieces, including runner mode, subprocess smoke, delivery adapters,
Feishu token smoke, service status, and service env. It still needs clearer
guidance for Docker readiness, Feishu service credentials, installed service env
linkage, overdue next runs, and missed-run history.

This milestone makes manual runs a true acceptance path for the cron runtime and
makes doctor output useful when the service does not advance jobs or delivery.

## Goals

1. Rework `agent cron run JOB_ID` so it creates one manual cron run and executes
   it through the same scheduler, runner, output, and delivery path used by
   service ticks.
2. Preserve the job's periodic schedule when manually running it. A manual run
   must not move or recompute the normal `next_run_at` for the schedule.
3. Keep `agent cron run JOB_ID --dry-run` non-mutating, but make its plan reflect
   the real manual-run decision path.
4. Preserve existing scheduler tick behavior for automatic service execution.
5. Extend `cron doctor` with actionable diagnostics for Docker, Feishu,
   `service.env`, installed service environment linkage, next due jobs, and
   missed runs.
6. Keep diagnostics concrete: warnings and failures should include the next
   command or configuration change the user can apply.

## Non-Goals

- Do not replace the scheduler with a separate manual-run executor.
- Do not change existing cron schedule parsing.
- Do not make manual run advance, skip, or repair the periodic schedule unless a
  concurrency policy explicitly affects the manual run itself.
- Do not add new delivery adapters in this milestone.
- Do not require Docker for development profiles that intentionally use local
  execution.
- Do not make Feishu credentials mandatory when no active Feishu delivery job
  exists.
- Do not add interactive credential prompts.
- Do not change the OS service install lifecycle beyond diagnostics.

## Manual Run Semantics

`agent cron run JOB_ID` should mean:

> Run this job once now through the real cron runtime, record the run, save its
> output, and deliver the same notification the service would deliver.

The command should be implemented by creating a manual run claim and then
invoking the scheduler path. The exact API can be shaped during implementation,
but the design intent is a `StateStore` operation such as:

```text
StateStore.claim_manual_job(job_id, now_text, requested_by="cli")
```

The manual claim should:

- validate the job exists
- reject disabled or completed jobs unless existing CLI semantics explicitly
  allow them
- resolve concurrency key and policy using the same normalization as automatic
  claims
- create a run with enough metadata to distinguish manual origin from scheduled
  origin
- make the run available to the next scheduler tick
- leave the job's periodic `next_run_at` unchanged

The scheduler should then execute that manual claim through the same execution
path as automatic claims. The resulting run should use the existing state
machine and output/delivery records.

## Scheduler Integration

`cron.scheduler.tick()` remains the single production entry point.

The tick order should stay compatible with the current service behavior:

1. Resolve the tick time.
2. Acquire the process-local tick lock.
3. Recover expired leases.
4. Complete stale runs when idle timeout or heartbeat rules require it.
5. Recover stale delivery events.
6. Dispatch pending or retryable non-origin delivery events.
7. Promote queued runs.
8. Claim due scheduled jobs.
9. Execute all claimed runs, including manual claims.
10. Save run output and enqueue run deliveries.
11. Dispatch newly enqueued delivery events when supported by the existing
    delivery tick flow.

If implementation needs a new helper, it should be small and explicit, for
example:

```text
StateStore.claim_manual_job()
StateStore.claim_ready_manual_runs()
```

Manual runs should not require a second runner abstraction. They should use the
same `JobRunner` dependency passed to `tick()` and therefore work with both
inprocess development runs and subprocess production runs.

## Concurrency Behavior

Manual runs are real runs and should respect the job's concurrency policy:

- `skip_if_running`: create a skipped run with a clear exit reason when another
  active run occupies the key.
- `queue_one`: queue one manual run when an occupying run exists, unless a queued
  run for the key already exists.
- `queue_all`: queue the manual run when the key is occupied.
- `replace_running`: abandon currently active runs for the key and execute the
  manual run.

The command output should tell the user when the requested run was queued,
skipped, or used replacement behavior. It should not report success merely
because a run record was created; it should distinguish execution success from
scheduling outcome.

## CLI Output

For a non-dry manual run, the CLI should print a compact acceptance summary:

```text
Ran cron job <job_id>.
Run: <run_id>
Status: ok|failed|skipped|queued|abandoned
Output: <path or ->
Delivery: delivered=<n> failed=<n> pending=<n> dead=<n>
```

If the job failed, include the first concise error line and preserve the output
path for inspection.

If delivery failed or is pending retry, show the delivery event status and point
to:

```bash
python -m agent_cli cron deliveries <run_id>
```

For `--dry-run`, keep the command non-mutating and include:

- decision
- whether the job can be manually claimed now
- concurrency policy and predicted outcome
- delivery targets
- runner mode and timeout
- current periodic `next_run_at`
- confirmation that the periodic schedule will not be advanced by a manual run

## Doctor Diagnostics

`cron doctor` should continue returning exit code `0` for all-ok, `1` for
warnings, and `2` for failures. New checks should prefer warnings when the
system can still run in some mode and failures when a configured active path is
known to be broken.

### Docker

Doctor should inspect Docker only when the effective runtime is likely to use
Docker. This includes hosted/prod profiles where the terminal backend defaults
to Docker, or explicit Docker configuration discovered through existing profile
or terminal environment helpers.

Checks:

- effective runtime profile
- whether Docker is expected for the active terminal backend
- `docker` command availability
- `docker version` success
- daemon connection or permission failure

Actionable messages:

- If Docker is missing: install Docker or switch the runtime/backend to local for
  development.
- If `docker version` fails: start Docker Desktop or the Docker daemon, or fix
  user permission to access the daemon.
- If production profile uses local execution unexpectedly: confirm the profile or
  set the intended backend explicitly.

### Feishu

Doctor should keep the existing Feishu target and token validation, then make
service-readiness explicit for active Feishu cron jobs.

Checks:

- active Feishu cron job count
- non-empty Feishu chat id
- target validation through the gateway adapter
- `FEISHU_APP_ID` and `FEISHU_APP_SECRET` in `service.env`
- shell Feishu env presence when service env is missing
- token smoke result

Actionable messages:

```bash
python -m agent_cli cron service env set FEISHU_APP_ID <app_id>
python -m agent_cli cron service env set FEISHU_APP_SECRET <app_secret>
python -m agent_cli cron service restart
```

On launchd, doctor should also mention reinstalling or refreshing the service
when embedded service env values are stale.

### Service Env And Installed Service Linkage

Doctor should verify that service credentials are both present and reachable by
the installed service.

Checks:

- `service.env` path exists or is intentionally absent
- file readability
- restrictive permissions
- systemd unit references the expected `EnvironmentFile`
- systemd unit includes the expected `WorkingDirectory` and `PYTHONPATH`
- launchd plist has service env values expanded, or warns when stale

Actionable messages should point to:

```bash
python -m agent_cli cron service install --force
python -m agent_cli cron service restart
```

### Next Due And Missed Runs

Doctor should summarize whether automatic scheduling appears to be advancing.

Checks:

- next due active job
- whether the next due time is already in the past
- service installed/active/heartbeat freshness
- latest tick time
- recent `missed_run` records
- jobs that are active but have no `next_run_at`

Actionable messages:

- If next due is in the past and the service heartbeat is stale, suggest
  `agent cron service start` or `agent cron service restart`.
- If next due is in the past and the service is fresh, suggest inspecting
  `agent cron service status`, `agent cron status`, and latest runs.
- If recent `missed_run` records exist, explain that the scheduled window was
  missed and point to `agent cron runs JOB_ID`.
- If no job is due but the user wants an immediate acceptance test, suggest
  `agent cron run JOB_ID`.

## Legacy Reference

Reference implementation keeps cron status understandable by answering three operational
questions: whether the gateway is running, how many active jobs exist, and when
the next run is due. This project has a richer runtime than reference implementation because it
tracks jobs, runs, deliveries, service status, leader leases, and service env in
separate persistent stores.

The design should preserve that richer state model while adopting the same
operator-facing clarity:

- automatic scheduling should be visibly ready or not ready
- active jobs and next due time should be prominent
- failures should point to the subsystem that blocks execution or delivery

## Acceptance Criteria

1. `agent cron run JOB_ID` creates a run record visible through
   `agent cron runs JOB_ID`.
2. The manual run saves output and shows the output path in CLI output.
3. Manual run delivery creates delivery events linked to the run.
4. Feishu delivery from a manual run can succeed, fail, or retry through the same
   delivery dispatcher used by service ticks.
5. A failed manual job still sends or queues the configured failure notification.
6. Manual run does not change the job's periodic `next_run_at`.
7. Manual run respects `replace_running`, `skip_if_running`, `queue_one`, and
   `queue_all` policies through integration tests.
8. `--dry-run` does not create runs, outputs, or deliveries.
9. Doctor reports actionable Docker diagnostics when the active runtime expects
   Docker and Docker is missing or unusable.
10. Doctor reports actionable Feishu service env diagnostics for active Feishu
    cron jobs.
11. Doctor reports installed service env linkage problems and gives reinstall or
    restart commands.
12. Doctor reports overdue next due jobs and recent `missed_run` records with
    suggested inspection commands.

## Test Strategy

Add focused tests at three levels:

1. State store tests for manual claim creation, periodic `next_run_at`
   preservation, and concurrency outcomes.
2. Scheduler or E2E cron tests proving manual claims go through output and
   delivery while service-style ticks still process due jobs and delivery retry.
3. CLI tests for `cron run`, `cron run --dry-run`, and doctor diagnostics.

Tests should avoid OS service mutation, real Docker, real Feishu network calls,
and long sleeps. Docker checks and Feishu smoke should be injectable or
monkeypatched so doctor can be verified deterministically.

