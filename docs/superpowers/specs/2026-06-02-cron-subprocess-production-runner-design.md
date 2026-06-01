# Cron Subprocess Production Runner Design

## Context

The cron runtime already has an optional subprocess runner boundary:

- `cron.runner_client` selects `inprocess` or `subprocess` from
  `AGENT_CRON_RUNNER_MODE`.
- `cron.runner_subprocess` starts `python -m cron.runner_worker` per job,
  enforces a parent-side timeout, captures bounded diagnostics, and cleans up
  its per-run temp directory.
- `agent cron doctor` checks runner mode and, when subprocess mode is active,
  verifies the worker entrypoint, subprocess timeout, and runner temp directory
  writability.
- user-level systemd and launchd service installation already propagates
  selected environment variables into the installed service.

Hermes is the runtime reference: a long-lived cron process should schedule and
supervise work, while actual agent execution should cross an isolation boundary
so crashes, file descriptors, terminal subprocesses, browser daemons, and
cached client state do not accumulate in the scheduler process.

This phase promotes the existing subprocess runner from optional capability to
the production-recommended service path while preserving the current development
default.

## Goals

1. Keep local development behavior unchanged: unset `AGENT_CRON_RUNNER_MODE`
   resolves to `inprocess`.
2. Make hosted/prod user-level service installation default to subprocess mode
   when no explicit runner mode is already configured.
3. Make `agent cron doctor` able to prove the subprocess worker protocol works
   without running a real agent job or LLM call.
4. Surface actionable diagnostics for runner mode, effective profile,
   subprocess timeout, worker smoke, and runner temp cleanup.
5. Keep doctor safe by default; cleanup of old runner temp directories requires
   an explicit flag.

## Non-Goals

- Do not make subprocess mode the global default for foreground `agent cron
  serve`, manual ticks, or development profiles.
- Do not implement a persistent worker daemon.
- Do not run a real cron job, model call, or delivery dispatch as part of
  default doctor checks.
- Do not parse systemd units or launchd plists as the primary status source in
  this phase.
- Do not change scheduler tick semantics beyond runner selection and
  diagnostics.

## Profile and Runner Mode Policy

Runner mode precedence:

1. If `AGENT_CRON_RUNNER_MODE` is explicitly set, use it as the effective mode.
2. Otherwise, use `inprocess`.
3. During user-level service install only, if the effective production signal is
   hosted/prod and no explicit runner mode is set, inject
   `AGENT_CRON_RUNNER_MODE=subprocess` into the installed service environment.

Production signal:

- `AGENT_RUNTIME_PROFILE in {"hosted", "prod"}` is the authoritative runtime
  posture signal.
- CLI `--profile hosted` or `--profile prod` is a compatibility signal for
  service install and diagnostics, because existing users may naturally align
  named CLI profiles with deployment posture.
- If both are present and disagree, `AGENT_RUNTIME_PROFILE` controls automatic
  behavior. The CLI profile can still be displayed as context.

This means:

- `AGENT_RUNTIME_PROFILE=prod agent cron service install` installs a service
  with `AGENT_CRON_RUNNER_MODE=subprocess` unless the user already set
  `AGENT_CRON_RUNNER_MODE`.
- `python -m agent_cli --profile prod cron service install` does the same when
  `AGENT_RUNTIME_PROFILE` is unset.
- `python -m agent_cli --profile dev cron service install` preserves the current
  in-process default.
- `AGENT_CRON_RUNNER_MODE=inprocess AGENT_RUNTIME_PROFILE=prod agent cron
  service install` preserves the explicit in-process choice but doctor warns.

## Service Install Behavior

Add runner/profile awareness to the install path:

- `agent_cli.main` passes the parsed CLI profile name into
  `cron_commands.install_cron_service`.
- `agent_cli.main` also passes the parsed CLI profile name into
  `cron_commands.cron_doctor` and `cron_commands.cron_status` so diagnostics can
  explain whether a production-looking CLI profile influenced recommendations.
- `cron_commands.install_cron_service` passes service profile context into
  `cron.service_manager.install_service`.
- `ServiceInstallConfig` carries an environment override map for values that
  should be written into the installed service but not necessarily exported in
  the current parent process.
- `service_environment()` merges current supported environment variables with
  install-time overrides.

When hosted/prod auto-injection occurs:

- systemd units include
  `Environment="AGENT_CRON_RUNNER_MODE=subprocess"`.
- launchd plists include
  `EnvironmentVariables.AGENT_CRON_RUNNER_MODE = subprocess`.
- the install success message states that subprocess mode was installed for the
  hosted/prod profile.

When no auto-injection occurs:

- install behavior stays compatible with today.
- if the profile looks production-like but the user explicitly chose
  `inprocess`, installation succeeds and the message should mention the explicit
  runner mode so the choice is visible.

## Doctor Diagnostics

`agent cron doctor` should always report:

- effective runtime profile: `AGENT_RUNTIME_PROFILE` if set, otherwise CLI
  profile compatibility signal if provided, otherwise `dev`.
- runner mode and whether it is supported.
- whether hosted/prod posture is using the recommended subprocess mode.

For subprocess mode, doctor should additionally report:

- subprocess timeout validity from `AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT`;
- a warning if the timeout is suspiciously small, with an initial threshold of
  less than 30 seconds;
- worker protocol smoke result;
- runner temp directory writability;
- runner temp residual count and oldest residual age.

Unsupported runner mode is a failure. Hosted/prod with effective in-process mode
is a warning unless the runner mode value itself is invalid.

## Worker Protocol Smoke

Replace the current `python -m cron.runner_worker --help` doctor check with a
real protocol smoke that does not execute a cron job:

1. Create a secure temporary runner smoke directory under the runner temp root.
2. Write an input file with a protocol version and smoke marker.
3. Start `python -m cron.runner_worker --smoke --input <input> --output <output>`
   with a short smoke timeout.
4. The worker validates the input file, writes a versioned success result, and
   exits zero.
5. The parent validates the result file and removes the smoke directory.
6. Doctor reports success only if the child process, result protocol, and
   cleanup all succeed.

The smoke path must not import or construct the real agent runner. It verifies
the subprocess boundary, Python module resolution, temp file permissions, result
file protocol, and cleanup.

## Runner Temp Cleanup

Default doctor behavior is observation only:

- Count child directories under `runner-tmp`.
- Ignore the active smoke directory.
- Report a warning when old residual directories exist.
- Include the oldest residual age when available.

Add an explicit CLI option:

```bash
python -m agent_cli cron doctor --cleanup-runner-tmp
```

Cleanup behavior:

- only deletes directories under `get_runner_tmp_dir()`;
- only deletes directories older than 24 hours by default;
- only deletes entries matching the runner-created directory shape: regular
  directories whose name is a 16-character hex run id or a documented smoke
  directory prefix;
- reports how many directories were removed and how many remain;
- failure to delete one residual directory is a warning unless the temp root is
  unreadable or unwritable.

The cleanup option is intentionally explicit because doctor is otherwise a safe,
read-mostly diagnostic command.

## Status Output

`agent cron status` should include:

- effective profile;
- runner mode with supported/unsupported marker;
- subprocess timeout when the effective mode is subprocess;
- runner temp residual summary.

`agent cron service status` remains focused on service manager and heartbeat
state. It can continue displaying last tick and delivery summaries from the
service status file. Parsing installed unit/plist environment can be added later
if users need drift detection between the current shell and installed service.

## Error Handling

- Invalid `AGENT_CRON_RUNNER_MODE` remains a scheduler-level failed run result
  when a job is executed, and is a doctor failure.
- Invalid subprocess timeout falls back to the runtime default for execution but
  is a doctor failure so operators fix the configuration.
- Worker smoke timeout is a doctor warning if the worker might merely be slow,
  and a failure if the process exits nonzero or writes an invalid result.
- Temp cleanup failures are warnings for individual directories and failures for
  an unusable temp root.

## Testing

Unit tests should cover:

- runner mode policy for explicit env, runtime profile, CLI profile, and dev
  default;
- systemd render includes auto-injected subprocess mode for hosted/prod service
  installs;
- launchd render includes auto-injected subprocess mode for hosted/prod service
  installs;
- explicit `AGENT_CRON_RUNNER_MODE=inprocess` is preserved in hosted/prod;
- `agent_cli.main` passes CLI profile into cron service install;
- doctor warns for hosted/prod in-process mode;
- doctor runs worker protocol smoke in subprocess mode;
- doctor reports invalid timeout and too-small timeout;
- doctor reports residual runner temp directories without deleting by default;
- `--cleanup-runner-tmp` deletes only old valid runner temp directories.

Focused integration tests should run:

```bash
python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py
python -m pytest tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py
```

## Acceptance Criteria

1. Dev/default installs keep `inprocess` behavior unless the user explicitly
   sets subprocess mode.
2. Hosted/prod service installs write subprocess mode into the installed
   user-level service when no explicit runner mode exists.
3. Doctor can detect an invalid runner mode, invalid timeout, production
   in-process mismatch, worker protocol failure, unwritable runner tmp root, and
   old runner tmp residuals.
4. Doctor default checks do not delete files; cleanup requires
   `--cleanup-runner-tmp`.
5. The implementation remains compatible with systemd-user and launchd-user
   service backends.
