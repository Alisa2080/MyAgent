# Permission Smoke Test Summary

## Runtime profiles
- Supported profiles: `dev`, `test`, `hosted`, `prod`.
- `AGENT_RUNTIME_PROFILE` controls the default runtime posture.
- If `TERMINAL_ENV` is unset, `dev`/`test` default to `local`, and `hosted`/`prod` default to `docker`.

## Workspace writes
- Workspace-local `write_file` and `patch` operations are allowed without human review.

## Writes outside the workspace
- Ordinary writes outside the workspace require one-shot approval / review.

## Sensitive path hard deny
- Sensitive write targets are denied.
- Sensitive examples include `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.azure`, `~/.config/gh`, files like `~/.netrc`, and container/system paths such as `/etc` and Docker socket paths.
- Shell commands that read from or write to sensitive paths are denied.

## Shell command risk grading
- Low-risk read-only commands and test commands are allowed.
- Risky commands require review / approval, including:
  - package installs
  - network commands
  - destructive commands
  - permission changes
  - background / long-running server commands
  - complex shell
- Hardline destructive commands are denied.

## Docker temporary network lease
- In `hosted`/`prod`, Docker sandboxes default to no network.
- Approved network commands receive temporary network access only for the lifetime of that command.
