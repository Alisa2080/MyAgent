# Local And Docker Terminal Backends Design

## Goal

Simplify the execution environment subsystem so it supports only the `local`
and `docker` terminal backends. Remove the SSH and Singularity implementations
and reject every unsupported explicit `TERMINAL_ENV` value during configuration
resolution.

This is an intentional breaking change. An old SSH or Singularity
configuration must fail clearly rather than silently execute commands on the
host or in Docker.

## Scope

The change covers the active terminal backend implementation and all current
code, tests, and runtime documentation that depend on its supported backend
set.

It includes:

- terminal environment configuration and validation;
- environment construction and requirement checks;
- terminal and background process integration;
- file-tool backend path handling;
- current README and operational documentation;
- tests for configuration, terminal environments, file operations, and code
  execution.

Historical documents under `docs/superpowers/specs` and
`docs/superpowers/plans` remain unchanged. They describe decisions and
implementation state at the time they were written and are not part of the
active runtime contract.

## Supported Backend Contract

The complete supported backend set is:

- `local`
- `docker`

`agent_core.permissions.profiles.resolve_terminal_env()` is the single
validation boundary for this contract.

When `TERMINAL_ENV` is explicitly set, the resolver:

1. strips leading and trailing whitespace;
2. converts the value to lowercase;
3. returns it when it is `local` or `docker`;
4. otherwise raises `ValueError`.

The error identifies the rejected value and lists the two supported values. A
stable message should follow this form:

```text
Unsupported TERMINAL_ENV 'ssh'. Supported values: local, docker.
```

This rule applies equally to removed values such as `ssh` and `singularity`,
spelling mistakes, and hypothetical backend names such as `podman`.

An unset or whitespace-only `TERMINAL_ENV` retains the existing profile-based
defaults:

- `dev` and `test` use `local`;
- `hosted` and `prod` use `docker`.

There is no fallback after an unsupported explicit value. In particular, an
old remote configuration cannot cause commands to run locally.

## Terminal Toolkit Simplification

Delete the backend implementation modules:

- `agent_tools/terminal_toolkit/environments/ssh.py`
- `agent_tools/terminal_toolkit/environments/singularity.py`

Simplify `agent_tools/terminal_toolkit/terminal_tool.py` to contain only the
local and Docker paths:

- remove SSH and Singularity imports;
- remove their environment factory branches;
- remove SSH connection configuration and Singularity image configuration;
- remove backend-specific image selection, disk checks, and requirement checks;
- remove internal parameters and helpers that exist only to construct those
  backends;
- update unsupported-backend messages to list only `local` and `docker`.

The environment factory remains an explicit two-branch implementation. A
backend registry is unnecessary for two stable implementations and would add
an abstraction without simplifying current behavior.

Docker lifecycle, persistence, network policy, volume forwarding, workspace
mounting, and host-user options remain unchanged. Local process behavior and
profile defaults also remain unchanged.

## File Toolkit Simplification

The file toolkit currently carries path and capability distinctions for local,
Docker, SSH, and Singularity environments. Reduce this model to:

- `local`: paths are resolved and constrained against host workspace rules;
- `docker`: paths are resolved against the active/configured container
  directory, with the existing `/workspace` behavior.

Remove:

- SSH remote-home resolution;
- SSH active/configured cwd exceptions;
- Singularity active/configured cwd exceptions;
- class-name inference for removed backend types;
- comments and docstrings that claim support for removed or unrelated
  backends.

Tests that use SSH or Singularity merely as examples of a generic non-local
backend should be converted to Docker. Tests that specifically verify removed
backend semantics should be deleted.

The path-policy cleanup must not weaken the existing Docker or local safety
rules. Sensitive host paths, Docker socket paths, workspace boundaries, and
skill-cache restrictions remain enforced.

## Error Propagation

All code paths that resolve terminal configuration inherit the same
`ValueError` behavior from `resolve_terminal_env()`. This includes terminal
execution, file tools, code execution, diagnostics, and requirement checks.

Callers may translate the exception into their existing public tool or CLI
error format, but they must not replace it with a fallback backend.

The lower-level environment factory may retain a defensive error for direct
internal misuse. Its message lists only `local` and `docker`; normal runtime
configuration should fail earlier in `resolve_terminal_env()`.

## Documentation

Update current runtime documentation to state that only `local` and `docker`
are supported and remove active configuration references to:

- `TERMINAL_SSH_HOST`
- `TERMINAL_SSH_USER`
- `TERMINAL_SSH_PORT`
- `TERMINAL_SSH_KEY`
- `TERMINAL_SINGULARITY_IMAGE`

The primary files are:

- `README.md`
- `agent_tools/terminal_toolkit/README.md`
- `agent_tools/file_toolkit/README.md`
- `docs/permission-smoke-test.md`, if its active backend guidance requires an
  update

Historical specs and plans are explicitly excluded from documentation cleanup.

## Testing Strategy

Configuration tests cover:

- explicit `local`;
- explicit `docker`;
- normalization of case and surrounding whitespace;
- profile defaults when the variable is unset or blank;
- `ValueError` for `ssh`, `singularity`, misspellings, and other unknown
  values;
- an error message that includes the rejected value and supported values.

Terminal toolkit tests cover:

- local and Docker environment construction;
- local and Docker requirement checks;
- active environment reuse and cleanup;
- the defensive factory error for unsupported direct input;
- unchanged Docker lifecycle and network behavior.

File toolkit tests cover:

- host workspace behavior for local;
- container cwd and `/workspace` behavior for Docker;
- safe-root and sensitive-path enforcement for both supported backends;
- removal or Docker conversion of tests that previously modeled SSH or
  Singularity.

Code execution tests that use an SSH marker only to represent a non-local
environment should use Docker. No current test should import, instantiate, or
advertise the removed backend classes.

A scoped residual-reference check should verify that active Python code and
current runtime documentation no longer expose SSH or Singularity as execution
backends. Security references such as `.ssh` sensitive-path denial remain
valid and must not be removed. Historical specs and plans are excluded from
this check.

## Acceptance Criteria

- `local` and `docker` are the only accepted explicit terminal backend values.
- Any other explicit value raises `ValueError` during
  `resolve_terminal_env()`.
- Unset configuration keeps the existing profile-based defaults.
- SSH and Singularity implementation modules, imports, factory branches,
  configuration keys, and requirement checks are removed.
- Active file-tool path policy contains only local and Docker backend behavior.
- Current runtime documentation advertises only local and Docker.
- Historical design and plan documents remain unchanged.
- Local and Docker terminal, file-tool, permission, and code-execution tests
  pass.
- No silent fallback can move an unsupported backend request to local or
  Docker execution.

## Non-Goals

- Adding Podman or another replacement backend.
- Introducing a general backend plugin or registration framework.
- Changing Docker isolation, networking, persistence, or workspace mounting.
- Changing local execution semantics.
- Rewriting historical design and implementation records.
