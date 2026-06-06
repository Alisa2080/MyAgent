# Layered Permissions Design

Date: 2026-05-21

## Goal

Implement a standard agent permission model for this project:

- Default to low privilege.
- Allow normal code reading and workspace edits without interrupting the user.
- Route shell commands through sandbox-aware policy.
- Ask for approval when a tool call crosses workspace, network, or risk boundaries.
- Hard-deny sensitive paths and destructive commands that should not be approved.

The first implementation targets the existing LangChain public tool wrappers,
terminal toolkit, file toolkit, and human-in-the-loop middleware.

## Current Baseline

The project already has several safety mechanisms:

- `agent_tools/shared/file_policy.py` sets `TERMINAL_CWD` and
  `AGENT_WRITE_SAFE_ROOT` to the workspace.
- `agent_tools/public/files.py` enforces workspace-aware read/write admission.
- `agent_tools/file_toolkit/backend_paths.py` understands local, Docker,
  Singularity, and SSH backend path roots.
- `agent_tools/terminal_toolkit/approval.py` blocks hardline dangerous
  commands and flags dangerous command patterns.
- `agent_core/builders.py` configures human-in-the-loop review for terminal,
  process, write, patch, memory, and skill changes.
- `agent_tools/terminal_toolkit` supports `local`, `docker`,
  `singularity`, and `ssh` execution backends.

The missing piece is a unified policy engine that decides when a tool call is
automatically allowed, requires review, or is denied.

## Runtime Profiles

Add `AGENT_RUNTIME_PROFILE` with these values:

- `dev`: local development; default terminal backend is `local`.
- `test`: automated tests or CI; default terminal backend is `local`.
- `hosted`: hosted agent runtime; default terminal backend is `docker`.
- `prod`: production service runtime; default terminal backend is `docker`.

Resolution order:

1. Use explicit `AGENT_RUNTIME_PROFILE` when set.
2. Otherwise infer:
   - production indicators such as `ENVIRONMENT=production`,
     `APP_ENV=prod`, or `NODE_ENV=production` map to `prod`;
   - hosted indicators such as `AGENT_HOSTED=true` or deployment-specific
     LangGraph/LangSmith server variables map to `hosted`;
   - CI indicators such as `CI=true` or `GITHUB_ACTIONS=true` map to `test`.
3. Fall back to `dev`.

If `TERMINAL_ENV` is explicitly set, respect it. If it is not set, derive the
default from the resolved profile: `dev` and `test` use `local`; `hosted` and
`prod` use `docker`.

Hosted and production runtimes must not mount host credentials or Docker socket
paths by default.

## Policy Engine

Create a central policy module under `agent_core/permissions/`:

- `profiles.py`: profile resolution and terminal backend defaults.
- `models.py`: policy decision dataclasses and risk tags.
- `file_policy.py`: read/write path classification, workspace checks, and
  sensitive path detection.
- `command_policy.py`: shell command classification.
- `network_policy.py`: profile-aware network decisions.
- `audit.py`: structured logging for review, approval, and denial events.

Policy decisions use three outcomes:

- `allow`: tool call may execute without user interruption.
- `review`: tool call must enter human approval.
- `deny`: tool call must not execute.

The policy engine is used in two places:

- Human-in-the-loop middleware decides which generated tool calls interrupt.
- Public tool wrappers re-check hard boundaries before execution.

The low-level file and terminal toolkits remain defensive backstops.

Approval state should be carried in a runtime-scoped approval registry keyed by
tool call id and task id. Tool schemas should not need user-visible approval
fields. If a framework path safely supports hidden metadata on tool calls, it
may mirror the registry data, but the registry remains the source of truth.

## File Permissions

Read-like tools are automatically allowed when existing read admission permits
them:

- `read_file`
- `search_files`
- `list_directory`
- `file_info`

Existing protections stay in place, including workspace path checks and direct
read blocking for internal skill cache paths.

Write-like tools are classified as follows:

- Workspace paths: `allow`.
- Ordinary paths outside the workspace, such as `/tmp/report.txt`: `review`.
- Sensitive paths: `deny`.

Sensitive path detection covers host and backend paths:

- home credential directories: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`,
  `~/.docker`, `~/.azure`, `~/.config/gh`;
- secret/config files: `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.pgpass`;
- system paths: `/etc`, `/var/run/docker.sock`, `/run/docker.sock`;
- container equivalents such as `/root/.ssh`, `/root/.aws`, `/root/.kube`,
  `/root/.docker`, and `/etc`.

For `write_file` and `patch`, middleware may approve ordinary workspace escape
for a single call. The wrapper must verify that the approval token matches the
tool name, risk tags, task id, and argument digest before permitting execution.
Sensitive paths are never approvable.

## Shell Command Permissions

Commands are classified by `command_policy`.

Automatically allowed examples:

- read/status commands: `pwd`, `ls`, `find`, `rg`, `grep`, `cat`, `head`,
  `tail`, `wc`, `sed -n`;
- read-only Git commands: `git status`, `git diff`, `git log`, `git show`,
  `git branch`;
- test commands: `pytest`, `python -m pytest`, `npm test`, `pnpm test`,
  `yarn test`, `cargo test`, `go test`;
- information commands: `python --version`, `node --version`, `which`,
  `command -v`.

Commands requiring review include:

- write indicators: `>`, `>>`, `tee`, `cp`, `mv`, `mkdir`, `touch`;
- deletion or permission changes: `rm`, `chmod`, `chown`;
- package/environment modification: `pip install`, `npm install`,
  `pnpm install`, `apt install`, `brew install`;
- network operations: `curl`, `wget`, `ssh`, `scp`, `git clone`,
  `git fetch`, `git pull`, `git push`;
- background or long-running server commands, including `background=True`,
  `npm run dev`, `vite`, `next dev`, `uvicorn`, and watcher-style commands;
- privilege or service control: `sudo`, `systemctl`, `service`;
- complex shell forms that cannot be confidently classified as read-only.

Commands are denied when they match hardline destructive behavior, including
root filesystem deletion, system directory deletion, disk formatting, fork
bombs, raw block-device writes, and shutdown or reboot commands.

Dangerous commands require review in both local and Docker environments.
Sandboxing does not make destructive workspace operations automatically safe.

`process` tool actions inherit the risk posture of the terminal session they
act on:

- `list`, `poll`, `log`, `wait`, `kill`, and `close` are allowed when the
  existing session belongs to the current runtime task id.
- `write` and `submit` require review when the target session was started from
  a reviewed command, an interactive shell, or a command whose stdin can change
  filesystem, network, or process state in a way policy cannot classify.
- Process actions remain denied when the session does not belong to the current
  runtime task id.

## Network Policy

Network behavior is profile-based:

- `dev` and `test`: keep current network behavior.
- `hosted` and `prod`: Docker sandboxes start without network access.

Commands needing network access receive `review` with `requires_network=true`.
After approval, the network grant applies only to that single tool call.

For Docker, implement temporary network access on the main task sandbox:

1. Connect the existing container to a configured egress network before command
   execution.
2. Run the approved command.
3. Disconnect the container in a `finally` block.

If enabling network fails, the command must not run. If disabling network fails,
the result must contain a high-priority warning and the environment must be
cleaned up or marked unreusable to avoid accidentally keeping an online sandbox.

The first version does not promise real temporary network isolation for `local`,
`ssh`, or `singularity`. In hosted and production profiles, Docker is the
supported backend for enforced network boundaries.

## Approval Tokens

Approvals are single-use and scoped to one tool call.

When a `review` decision is approved, middleware records approval metadata such
as:

- `policy_approval_id`
- `policy_decision_id`
- `approved_risk_tags`
- `allow_network_once`
- argument digest
- runtime task id
- tool call id

Wrappers validate this metadata before honoring reviewed actions. If the user
edits the tool call during review, policy is recomputed and the previous
approval is not reused.

No approval is cached across commands, sessions, or risk classes.

## Human-in-the-Loop Behavior

Replace static review behavior for file and terminal tools with policy-driven
behavior:

- `allow`: do not interrupt; execute normally.
- `review`: interrupt with a clear reason, risk tags, and command/path preview.
- `deny`: synthesize a tool message explaining the denial; do not execute the
  tool.

Memory and skill management can continue to use mandatory review unless they
are later brought into the policy engine.

## Auditing

Log structured events for every review, approval, and denial:

- profile
- tool name
- runtime task id
- decision
- risk tags
- reason
- command or path preview
- whether a human approved it
- whether one-shot network access was granted

Do not log full secrets, large command strings, or complete file contents.

## Failure Behavior

Reads should favor useful tool errors over approval interruptions.

Writes and shell commands fail closed:

- unclassified writes require review or are denied when sensitive paths are
  suspected;
- unclassified shell commands require review;
- path parsing or policy errors around sensitive locations deny execution.

## Testing Plan

Profile tests:

- explicit `AGENT_RUNTIME_PROFILE` wins;
- inference maps production, hosted, and CI signals correctly;
- fallback profile is `dev`;
- default terminal backend is `local` for `dev/test` and `docker` for
  `hosted/prod` when `TERMINAL_ENV` is unset.

File policy tests:

- workspace writes are allowed;
- ordinary workspace escapes require review;
- sensitive host and container paths are denied;
- Docker `/workspace` writes are allowed;
- approval tokens permit only the reviewed ordinary escape.

Command policy tests:

- read-only commands and test commands are allowed;
- package installs, network commands, writes, deletes, permissions changes,
  and background server commands require review;
- hardline destructive commands are denied;
- complex unknown shells require review.

Network tests:

- hosted/prod Docker starts without network;
- approved network commands enable network for one call;
- success, failure, timeout, and exceptions all disconnect network;
- disconnect failure cleans or invalidates the environment.

Middleware and wrapper tests:

- `allow` tool calls do not interrupt;
- `review` tool calls interrupt and receive single-use approval metadata;
- `deny` tool calls do not execute;
- direct wrapper calls without approval cannot perform reviewed actions.

## Out of Scope

- General-purpose semantic shell parsing beyond conservative command
  classification.
- Reliable network toggling for `local`, `ssh`, or `singularity`.
- Session-level or time-window approval grants.
- Mounting host secrets into hosted/prod sandboxes.
- Replacing low-level file and terminal toolkit safety checks.
