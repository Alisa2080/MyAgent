# Cron Service Context And Env Design

## Context

The cron runtime now works end to end for local user-level service execution:
`cron service install/start` can run scheduler ticks, the subprocess runner can
execute jobs, run output is saved, and Feishu delivery can send cron updates.

The recent Feishu smoke test exposed two operational gaps:

1. A development checkout is not automatically importable from an installed
   service. The generated systemd unit starts Python with `-m agent_cli.main`,
   but the unit does not set `WorkingDirectory` or `PYTHONPATH`, so a local
   checkout that has not been installed as a package can fail with
   `ModuleNotFoundError`.
2. Platform credentials are still tied to the current shell or systemd user
   manager environment. Feishu can work after
   `systemctl --user import-environment`, but that is not durable enough for a
   restart/login lifecycle and is hard for `doctor` to reason about.

This milestone makes the cron service installable from a development checkout
and gives service credentials a dedicated, inspectable home.

## Goals

1. Make `cron service install` write enough runtime context for a development
   checkout to import `agent_cli.main` without requiring `pip install -e .`.
2. Automatically locate the project root from the installed code path, using a
   directory that contains `pyproject.toml` as the primary marker.
3. Add a persistent service env file under the cron home:

   ```text
   ~/.terminal-toolkit/cron/service.env
   ```

4. Add non-interactive service env management commands:

   ```bash
   python -m agent_cli cron service env set FEISHU_APP_ID cli_xxx
   python -m agent_cli cron service env set FEISHU_APP_SECRET xxx
   python -m agent_cli cron service env list
   python -m agent_cli cron service env unset FEISHU_APP_SECRET
   ```

5. Extend `cron doctor` so it distinguishes current shell credentials from the
   service runtime credentials used by the background service.
6. Keep existing runner-profile behavior unchanged: dev can remain inprocess;
   prod/hosted continues to prefer subprocess.

## Non-Goals

- Do not add interactive secret entry or `--prompt` in this phase.
- Do not add automatic copying from the current shell into `service.env`.
- Do not store Feishu secrets in SQLite.
- Do not make Feishu credentials mandatory when no active Feishu delivery job
  exists.
- Do not implement a general secret manager integration.
- Do not add system-level service or `sudo` workflows.
- Do not change the Feishu outbound adapter semantics.

## Service Runtime Context

Add a platform-independent service runtime context used by service rendering and
doctor checks.

The context should include:

- `project_root`: the detected checkout root.
- `pythonpath`: a value that includes `project_root` and preserves an existing
  `PYTHONPATH` when present.
- `service_env_file`: `get_cron_dir() / "service.env"`.

Project root detection should start from a known module path in this repository
and walk upward until it finds `pyproject.toml`. If no marker is found, the
fallback should be conservative: use the current working directory only when it
also appears to contain the local package layout; otherwise omit the checkout
context and let doctor warn.

`cron service install` should use this context when rendering platform service
definitions.

## Systemd Behavior

The user systemd unit should include:

```ini
[Service]
WorkingDirectory=<project_root>
Environment="PYTHONPATH=<project_root>[:<existing PYTHONPATH>]"
EnvironmentFile=-<cron_home>/cron/service.env
ExecStart=<python> -m agent_cli.main cron serve --interval <N> --lease-seconds <N>
```

Requirements:

- The `EnvironmentFile` reference must be optional, so a missing env file does
  not prevent the service from starting.
- Feishu secrets must not be written as `Environment="FEISHU_APP_SECRET=..."`
  in the unit file.
- Existing service environment variables such as `PATH`, `AGENT_CRON_HOME`,
  `AGENT_RUNTIME_PROFILE`, and runner settings should keep their current
  behavior.
- If an existing unit lacks `WorkingDirectory`, the project-root `PYTHONPATH`,
  or the service env file reference, doctor should warn and suggest:

  ```bash
  python -m agent_cli cron service install --force
  ```

## Launchd Behavior

launchd does not provide a direct equivalent of systemd `EnvironmentFile`.
The LaunchAgent should still include:

- `WorkingDirectory`
- `EnvironmentVariables.PYTHONPATH`
- existing service environment variables

For `service.env`, the first implementation should read the env file during
install and expand its key/value pairs into `EnvironmentVariables`. Doctor
should make the platform difference explicit:

- If `service.env` changes after install on launchd, warn that the service must
  be reinstalled or refreshed for the plist to pick up the new values.
- Do not write secret values into diagnostic output.

This keeps macOS functional without inventing a wrapper process solely to load a
dotenv file.

## Service Env File

Path:

```text
~/.terminal-toolkit/cron/service.env
```

Format:

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

Rules:

- Keys must match uppercase environment variable syntax:
  `[A-Z_][A-Z0-9_]*`.
- Values are stored as dotenv-compatible single-line values.
- Newlines and carriage returns are rejected.
- Writes are atomic.
- File permissions should be restricted to the current user, preferably `0600`.
- Parent directory permissions should follow existing cron directory security
  helpers.

The env parser and writer should preserve unrelated keys managed by the user
where practical. It does not need to support advanced dotenv syntax such as
variable expansion, comments with quoting edge cases, multiline strings, or
export prefixes.

## CLI UX

Add a nested command group below the existing service commands:

```text
agent cron service env set KEY VALUE
agent cron service env unset KEY
agent cron service env list
```

Behavior:

- `set` creates `service.env` if needed, updates one key, secures the file, and
  prints a short success message.
- `unset` removes one key and prints whether it was removed.
- `list` prints keys in stable sorted order.
- Sensitive values should be masked. At minimum, keys containing `SECRET`,
  `TOKEN`, `PASSWORD`, or `KEY` should not print their full values.
- The command should remain non-interactive in this phase. It should not prompt
  for hidden input.

After `set` or `unset`, the CLI should remind the user that an already-running
service needs `cron service restart`. On launchd, it should also mention that a
reinstall or refresh may be required if the plist embeds env values.

## Doctor Behavior

`cron doctor` should add service-runtime checks without replacing existing
Feishu adapter validation.

New checks:

1. Service context:
   - project root detected
   - `WorkingDirectory` is present in the installed service definition
   - `PYTHONPATH` includes the detected project root
2. Service env file:
   - path exists or is absent
   - file is readable by the current user
   - permissions are not overly broad
3. Platform service env linkage:
   - systemd unit references `service.env`
   - launchd plist has env values expanded from `service.env` or warns that the
     plist may need refresh
4. Feishu active-job readiness:
   - if an active job has a Feishu target, `FEISHU_APP_ID` and
     `FEISHU_APP_SECRET` must exist in `service.env`
   - if the current shell has these variables but `service.env` does not,
     doctor warns that the foreground CLI environment does not guarantee
     background service delivery
   - current Feishu token smoke remains a credential/API validity check and can
     continue to use the current process environment

Suggested commands should be concrete:

```bash
python -m agent_cli cron service env set FEISHU_APP_ID <app_id>
python -m agent_cli cron service env set FEISHU_APP_SECRET <app_secret>
python -m agent_cli cron service install --force
python -m agent_cli cron service restart
```

## Acceptance Criteria

1. From an uninstalled development checkout, `cron service install --force`
   writes a service definition that can import `agent_cli.main`.
2. The generated systemd unit contains `WorkingDirectory`, a project-root
   `PYTHONPATH`, and an optional `EnvironmentFile` reference.
3. The generated systemd unit does not contain plaintext Feishu credentials.
4. `cron service env set/list/unset` manages
   `~/.terminal-toolkit/cron/service.env` with restricted permissions.
5. With active `feishu:<chat_id>` cron jobs, doctor warns when `service.env`
   lacks `FEISHU_APP_ID` or `FEISHU_APP_SECRET`.
6. Doctor explicitly distinguishes current shell Feishu env from service env.
7. Updating `service.env` and restarting the user service is enough for systemd
   service Feishu delivery to work after service restart.
8. Existing cron service install, status, runner mode, delivery, and Feishu
   adapter tests continue to pass.

## Testing

Add focused tests around the boundaries that caused the real failure:

- service context root detection from module paths
- systemd unit rendering for `WorkingDirectory`, `PYTHONPATH`, and
  `EnvironmentFile`
- launchd plist rendering for `WorkingDirectory`, `PYTHONPATH`, and env
  expansion
- service env file set/list/unset behavior, masking, invalid keys, invalid
  values, atomic writes, and file permissions
- doctor warnings for:
  - installed service missing runtime context
  - active Feishu job with missing service env
  - shell env present but service env absent
  - service env permissions too broad

Keep tests deterministic by patching paths, platform adapters, and Feishu token
smoke calls instead of relying on a live systemd or Feishu API.
