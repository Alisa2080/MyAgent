# Cron User Service Runtime Design

## Context

The cron scheduler already has the core pieces needed for reliable execution:
jobs, runs, and delivery events are stored in SQLite; `CronService` records
heartbeat and tick status; scheduler leader leases prevent duplicate service
leaders; run records capture activity, timeout, stale, and delivery state.

The remaining gap is operational. Today cron jobs do not fire automatically
unless an embedding application calls `start_cron_scheduler(...)` or a user keeps
`agent cron serve` running in the foreground. reference implementation is a useful reference for
the desired user experience: its cron commands warn when the gateway is not
running and point users to an install command. This project should apply that
logic to the existing cron service instead of reworking the scheduler.

The first service-runtime phase targets local user-level services. A normal user
should be able to install, start, stop, inspect, and uninstall automatic cron
scheduling without writing Python integration code or manually authoring
systemd/launchd files.

## Goals

1. Add a user-level cron service command group:
   - `agent cron service install`
   - `agent cron service uninstall`
   - `agent cron service start`
   - `agent cron service stop`
   - `agent cron service restart`
   - `agent cron service status`
   - `agent cron service logs`
2. Support Linux user systemd and macOS launchd as the first platforms.
3. Keep `agent cron serve` as the foreground/debug entrypoint used by installed
   services.
4. Surface service-manager state in `agent cron status`.
5. Extend `agent cron doctor` with concrete repair commands for missing,
   stopped, stale, or unsupported automatic scheduling.
6. Keep cron state recovery inside the existing scheduler and `StateStore`
   rather than adding state mutation to the service manager.

## Non-Goals

- Do not implement Linux system-level services or any `sudo` workflow.
- Do not implement Windows services.
- Do not implement multiple profile services in the first phase.
- Do not change the default runner mode from `inprocess` to `subprocess`.
- Do not modify jobs, runs, or delivery state from service-manager commands.
- Do not implement custom log rotation. Use journald on Linux; write bounded
  stdout/stderr files for launchd and read them from `logs`.

## CLI UX

The new command group sits below existing cron commands:

```text
agent cron service install [--force] [--interval 60] [--lease-seconds 180]
agent cron service uninstall
agent cron service start
agent cron service stop
agent cron service restart
agent cron service status
agent cron service logs [--lines 100]
```

`install` writes the platform service definition and configures it to start on a
future user login. It should not claim that the service is currently running;
users run `start` or `restart` for the current login session. It should not hide
failures from platform tools. If the unit or plist already exists and `--force`
is not set, it returns exit code 2 with a clear message.

`start`, `stop`, and `restart` only manage the platform service. They do not
call `cron.scheduler.tick()` and do not repair stored run state directly.

`status` answers four questions:

- Is this platform supported for user-level services?
- Is the cron service installed and enabled?
- Is the service currently active?
- Is the cron heartbeat fresh, and what happened in the last tick?

`logs` reads recent service output. On Linux this uses `journalctl --user`. On
macOS it tails the stdout/stderr paths configured in the LaunchAgent plist.

`agent cron status` remains the business-state view. It should add a compact
automatic-scheduling line such as:

```text
Service manager: systemd-user installed active enabled
Automatic scheduling: enabled
```

`agent cron doctor` should continue using `[ok]`, `[warn]`, and `[fail]` lines.
New checks should provide repair commands:

- not installed: `agent cron service install`
- installed but inactive: `agent cron service start`
- heartbeat stale: `agent cron service restart`
- unsupported platform: use `agent cron serve`

## Architecture

Add a thin service management layer. The scheduler and service loop stay
unchanged unless implementation finds a narrow integration bug.

```text
agent_cli/main.py
  -> agent_cli/cron_commands.py
    -> cron.service_manager
      -> cron.service_platforms.systemd_user
      -> cron.service_platforms.launchd_user
      -> cron.service_state / cron.leader / StateStore
```

### `cron.service`

Continues to own process behavior:

- install signal handlers
- acquire or renew scheduler leader lease
- write `service_status.json`
- call `cron.scheduler.tick()`
- release the leader lease on shutdown

Installed services should execute the existing foreground command:

```text
<python> -m agent_cli.main cron serve --interval <N> --lease-seconds <N>
```

### `cron.service_manager`

Owns platform-independent orchestration:

- detect the active user-service platform
- render service status as a structured object
- dispatch install/start/stop/restart/uninstall/logs operations
- compose heartbeat and scheduler summaries for CLI rendering

The service manager should not import heavy agent or runner dependencies.

### Platform Adapters

Each adapter exposes the same small interface:

```python
class CronServicePlatform:
    key: str
    supported() -> bool
    service_id(profile: str | None = None) -> str
    install(config: ServiceInstallConfig) -> ServiceCommandResult
    uninstall() -> ServiceCommandResult
    start() -> ServiceCommandResult
    stop() -> ServiceCommandResult
    restart() -> ServiceCommandResult
    status() -> ServiceRuntimeStatus
    logs(lines: int) -> ServiceCommandResult
```

The exact type names can change during implementation, but the boundary should
remain: adapters run platform commands and parse their output; CLI code formats
results for users.

## Platform Behavior

### Linux User Systemd

Use user-level systemd when `systemctl --user` is available.

- Unit name: `langchain-agent-cron.service`
- Unit path: `~/.config/systemd/user/langchain-agent-cron.service`
- Install flow:
  1. Create the user systemd directory.
  2. Write the unit file atomically.
  3. Run `systemctl --user daemon-reload`.
  4. Run `systemctl --user enable langchain-agent-cron.service`.
- Start/stop/restart:
  - `systemctl --user start langchain-agent-cron.service`
  - `systemctl --user stop langchain-agent-cron.service`
  - `systemctl --user restart langchain-agent-cron.service`
- Status:
  - `systemctl --user is-enabled`
  - `systemctl --user is-active`
  - `systemctl --user show --property=MainPID,ExecMainStatus,Result`
- Logs:
  - `journalctl --user -u langchain-agent-cron.service -n <N> --no-pager`

The unit should use the current Python executable and include enough environment
for the same cron home/profile to be used after login:

- `PATH`
- `AGENT_CLI_HOME` when set
- `AGENT_CRON_HOME` when set
- `AGENT_RUNTIME_PROFILE` when set
- runner-related variables that are already set, including
  `AGENT_CRON_RUNNER_MODE`, `AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT`, and
  `AGENT_CRON_MAX_PARALLEL`

### macOS Launchd

Use a user LaunchAgent.

- Label: `ai.langchain.agent.cron`
- Plist path: `~/Library/LaunchAgents/ai.langchain.agent.cron.plist`
- Install flow:
  1. Create `~/Library/LaunchAgents`.
  2. Write the plist atomically.
  3. Configure the plist with `RunAtLoad` so the service starts on future user
     logins.
  4. Do not require the service to be active immediately after install.
- Start/stop/restart should use an idempotent combination of `launchctl`
  commands selected during implementation and covered by command-construction
  tests.
- Logs:
  - configure `StandardOutPath` and `StandardErrorPath` under the cron log
    directory
  - `agent cron service logs` tails those files

The plist should pass the same command shape as systemd:

```text
<python> -m agent_cli.main cron serve --interval <N> --lease-seconds <N>
```

## Status Model

Service status combines three layers.

### Service Manager Status

- platform key: `systemd-user`, `launchd-user`, or `unsupported`
- supported: true/false
- installed: true/false/unknown
- enabled: true/false/unknown
- active: true/false/unknown
- pid when available
- last platform error when a platform query fails

### Cron Process Heartbeat

Read from existing `cron.service_state`:

- process state
- service pid
- leader state
- last heartbeat
- last tick started/finished
- last tick summary
- last service error
- exit reason

Freshness should use the existing heartbeat freshness helper. A stale heartbeat
means the platform service may be stopped, stuck, or writing to a different cron
home.

### Scheduler State

Read from existing `StateStore` and leader lease helpers:

- scheduler lease owner and expiry
- next due job
- running runs
- queued runs
- stale runs
- latest failed run
- delivery queue summary

## Recovery Behavior

Service management commands recover process lifecycle only.

- `restart` restarts the user service and lets the next tick recover expired
  leases and stale runs.
- `doctor` reports stale heartbeat and suggests `agent cron service restart`.
- The service manager never marks runs abandoned, clears job leases, retries
  delivery events, or deletes output.
- If the platform is unsupported, `install` and `start` return exit code 2 and
  suggest `agent cron serve` for foreground execution.
- If `service_status.json` exists but points at a dead pid, doctor should warn
  and recommend restarting the service. It should not delete the status file in
  the first phase.

## Error Handling

- Unsupported platform: exit code 2 for install/start/stop/restart/uninstall,
  with a foreground fallback command.
- Missing service definition on start/stop/restart: exit code 2 with
  `agent cron service install`.
- Platform command failure: exit code 1 and include the platform command's
  useful stderr/stdout excerpt.
- Existing service definition without `--force`: exit code 2.
- Malformed service status JSON: warn in status/doctor, continue showing
  platform state.
- Logs unavailable: return exit code 1 if the platform command fails; return
  exit code 0 with a clear "no logs yet" message if log files do not exist.

## Testing Plan

Tests should not require a real systemd or launchd environment.

### Unit Tests

- platform detection selects Linux user systemd, macOS launchd, or unsupported
  based on patched OS and command availability
- systemd install renders the expected unit command and environment
- systemd status parsing handles active, inactive, failed, and unknown states
- launchd install renders the expected plist command and environment
- launchd status parsing handles loaded, not loaded, running, and unknown states
- `--force` controls overwrite behavior for existing unit/plist files
- logs commands use `journalctl --user` on Linux and configured files on macOS

### CLI Tests

- parser accepts all `agent cron service ...` subcommands
- command handlers delegate to `cron.service_manager`
- `agent cron service status` includes platform, installed, enabled, active,
  heartbeat, and last tick
- `agent cron status` includes automatic scheduling summary
- `agent cron doctor` suggests install/start/restart/fallback commands for the
  right scenarios
- platform command failures propagate clear exit codes and messages

### Smoke Tests

- `agent cron serve --once` still runs through the existing `CronService`
- service status composition works with a fake fresh heartbeat and a fake stale
  heartbeat

## Rollout Notes

This phase should be additive. Existing users who call
`start_cron_scheduler(...)` from an embedding application or run
`agent cron serve` manually should not see behavior changes.

Documentation should state that user-level services generally start when the
user logs in. Linux machines that need cron before login, or after logout
without lingering enabled, are outside this first phase and should keep using a
custom deployment until system-level service support is designed.
