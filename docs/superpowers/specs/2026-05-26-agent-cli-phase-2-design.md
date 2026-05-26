# Agent CLI Phase 2 Design

Date: 2026-05-26

## Goal

Improve the project-native `agent_cli` without porting Hermes's full CLI stack.
Phase 2 focuses on three user-visible capabilities:

- richer human approval handling for LangGraph interrupts
- lightweight profile, dotenv, and config loading
- slash-command invocation for local skills

The CLI remains a plain terminal REPL backed by LangGraph SQLite checkpointing.
LangGraph checkpoints stay the conversation state source of truth, and the CLI
metadata table remains a lightweight session index.

## Non-Goals

- No full-screen TUI, approval panel, spinner, skin engine, banner system, or
  inline diff renderer.
- No Hermes sudo flow, gateway routing, plugin command framework, model picker,
  or background-task queue.
- No session-wide or time-window approvals.
- No tool-schema-aware form editor for approval edits.
- No automatic inlining of skill supporting files.
- No rewrite of `agent_core.model_config` or the project model configuration
  system.

## Current Context

Phase 1 already provides:

- `python -m agent_cli` and `python -m agent_cli.main`
- `chat`, `ask`, `sessions`, and `doctor`
- `PromptSession` input and slash command completion
- `/status`, `/title`, `/history`, and `/export`
- LangGraph SQLite checkpoint storage
- CLI session metadata in `cli_sessions`

The relevant runtime integration points are:

- `agent_cli/main.py`: argparse entrypoint, dotenv loading, logging setup, and
  command routing.
- `agent_cli/repl.py`: REPL orchestration, agent submission, slash command
  handling, and interrupt resume loop.
- `agent_cli/interrupts.py`: interrupt payload extraction and resume value
  construction.
- `agent_cli/commands.py`: central command registry.
- `agent_cli/input.py`: PromptSession and slash completer wiring.
- `agent_cli/paths.py`: CLI home and database path resolution.
- `agent_core/builders.py`: agent construction and
  `FlexibleHumanInTheLoopMiddleware` registration.
- `agent_core/human_loop.py`: project adapter around LangChain
  `HumanInTheLoopMiddleware`.
- `agent_tools/public/skills.py`: local skill metadata and skill content access.

## Architecture

Keep the small Phase 1 layout and add focused helpers:

- `agent_cli/approval.py`: terminal interaction for action request decisions.
- `agent_cli/config.py`: lightweight profile-aware config and dotenv helpers.
- `agent_cli/skill_commands.py`: skill slash command discovery and invocation
  message construction.

Existing modules continue to own their current boundaries:

- `main.py` owns early profile application, config loading, and CLI object
  construction.
- `paths.py` owns CLI home path resolution.
- `repl.py` owns the REPL loop and calls approval/skill helpers.
- `commands.py` remains the single source for built-in commands.
- `input.py` consumes both built-in and dynamic skill commands for completion.

Hermes reference files under `agent_cli/_hermes_reference` stay reference-only.
Phase 2 borrows patterns, not source-level structure.

## Approval UX

Replace the MVP global prompt:

```text
Approve? [y/N]:
```

with a per-action request decision flow.

The CLI should parse LangGraph interrupt payloads that look like:

```python
{
    "__interrupt__": [
        {
            "value": {
                "action_requests": [...],
                "review_configs": [...],
            }
        }
    ]
}
```

`action_requests` and `review_configs` must preserve positional pairing. Unknown
or partial payloads should still render safely with best-effort summaries.

For each request, show:

- current item index and total count
- tool name
- compact args summary
- risk or review description from the matching review config when available

Example:

```text
[1/3] terminal
Args: command="rm -rf build"
Risk: Destructive shell command requires approval.

Decision [y/n/e/r/a/q]:
```

Decision shortcuts:

- `y`: approve current request
- `n`: reject current request, then prompt for a rejection message
- `e`: edit current request arguments as JSON
- `r`: respond to the agent without executing the tool
- `a`: approve all remaining requests
- `q`: reject all remaining requests, then prompt for a shared rejection message

The resume value must match LangChain HITL's decision contract:

```python
{"decisions": [{"type": "approve"}]}
{"decisions": [{"type": "reject", "message": "..."}]}
{"decisions": [{"type": "edit", "args": {...}}]}
{"decisions": [{"type": "respond", "message": "..."}]}
```

### Edit Scope

`edit` is a JSON text MVP:

1. Pretty-print the current request args as JSON.
2. Prompt for replacement JSON.
3. Parse into a dict or list accepted by the installed LangChain middleware.
4. On parse failure, show the error and return to the same decision prompt.

The CLI does not open `$EDITOR`, validate against tool schemas, or perform
field-by-field editing. LangChain, tool wrappers, and `PolicyToolMiddleware`
remain responsible for final validation and execution-time policy enforcement.

### Middleware Fit

The approval UI must fit the existing
`agent_core.human_loop.FlexibleHumanInTheLoopMiddleware` contract.

The CLI should not implement Hermes sudo semantics. It only collects the human
decisions and resumes the LangGraph thread with `Command(resume=...)`.

The middleware remains responsible for:

- action request construction
- official decision processing
- AI message tool-call updates
- artificial `ToolMessage` creation for reject/respond
- approval/audit side effects for approved policy reviews

## Profile, Dotenv, and Config

Add a lightweight version of Hermes's startup-time environment isolation.

### CLI Home Resolution

Default behavior:

```text
AGENT_CLI_HOME if set
otherwise ~/.langchain-agent
```

Profile behavior:

```text
python -m agent_cli --profile dev
```

If `AGENT_CLI_HOME` is not already set, `--profile dev` maps CLI home to:

```text
~/.langchain-agent/profiles/dev
```

If `AGENT_CLI_HOME` is set, it remains authoritative and `--profile` only
records/display metadata. This avoids surprising users who explicitly provided
a home directory.

Profile names must be rejected unless they match:

```text
[A-Za-z0-9_.-]+
```

Empty names, path separators, and traversal such as `..` are invalid.

### Early Application

`agent_cli/main.py` should pre-parse `--profile` before normal command parsing
and before modules that depend on CLI home are asked for paths.

This is lighter than Hermes because the project already imports several
`agent_cli` modules at module import time. The Phase 2 requirement is that
database paths, history paths, log paths, config loading, and dotenv loading use
the profile-adjusted home during `main()`.

### Dotenv Loading

Load dotenv files in this order:

1. `<cli_home>/.env`
2. project root `.env` as development fallback

The project root is the current repository/workdir root used by CLI startup.
Existing environment variables should not be overridden by fallback files.

Doctor should report which dotenv paths are visible or loaded without printing
secret values.

### Config YAML

Read `<cli_home>/config.yaml` if it exists. Missing config is valid.

Supported Phase 2 keys:

```yaml
display:
  markdown: render
model:
  name: gpt-4.1
session:
  default_title: New session
```

Behavior:

- `--model` takes precedence over `model.name`.
- `model.name` is used as CLI metadata and passed through the existing CLI
  construction path. It does not rewrite `agent_core.model_config.MAIN_MODEL`.
- `session.default_title` is used for `/new` and sessions created without a
  first user message.
- `display.markdown` is parsed, validated, and stored on the CLI runtime
  settings object. Phase 2 does not add a full Markdown rendering mode switch.

Malformed YAML should produce a clear CLI startup error for `chat` and `ask`.
`doctor` should report it as a failed or warning health check rather than
crashing.

## Skill Slash Commands

Keep `/skills` and `/skill <name>`, and add dynamic slash commands for local
skills.

Do not copy Hermes `skill_commands.py`. The project already has skill metadata
and loading helpers in `agent_tools.public.skills`.

### Discovery

`agent_cli.skill_commands` should use existing skill metadata functions to
discover available skills.

Each skill can register:

- `/<skill-name>`
- the skill directory basename as an alias when different

Built-in commands win over skill commands. If a skill command conflicts with a
built-in command or alias, the skill remains visible through `/skills` and
`/skill <name>`, but the dynamic slash command is not registered.

### Completion and Help

Prompt completion should include dynamic skill commands after built-in commands.

`/skills` should show:

- skill name
- description
- direct slash command when registered
- conflict note when not registered

Built-in `/help` should include a concise line: "Skill commands are available
via `/skills`." Dynamic skill command details belong in `/skills`, not in the
main help output.

### Invocation

When the user enters:

```text
/python-debug fix this traceback
```

the CLI should load the skill content and submit a normal user message to the
agent:

```text
Use the following skill for this task.

<skill name="python-debug" dir="...">
...SKILL.md content...
</skill>

Supporting files:
- references/example.md
- scripts/helper.py

User request:
fix this traceback
```

Rules:

- The skill content comes from the existing skill loader.
- Supporting files are listed but not automatically inlined.
- If the skill cannot be loaded, return a normal command error and do not call
  the model.
- Empty prompt after the skill command is allowed; the agent receives an empty
  user request section.
- The injected message is persisted through the normal LangGraph checkpoint
  path as the user turn.

## Data Flow

### Startup

```text
python -m agent_cli --profile dev chat
  -> early profile pre-parse
  -> set AGENT_CLI_HOME if not already explicit
  -> load <cli_home>/.env
  -> load project .env fallback
  -> load <cli_home>/config.yaml
  -> setup logging/session store/checkpointer
  -> start REPL
```

### Approval

```text
agent result with __interrupt__
  -> extract action requests + review configs
  -> prompt for one decision per request
  -> build {"decisions": [...]}
  -> runner(agent, Command(resume=...), {"configurable": {"thread_id": ...}})
  -> repeat while result still has interrupts
```

### Skill Command

```text
/skill-name prompt text
  -> dynamic skill command lookup
  -> load skill content and supporting file list
  -> build skill invocation user message
  -> submit_message(message)
  -> normal checkpoint/session/interrupt handling
```

## Error Handling

- Invalid profile names exit with code `2` and a concise stderr message.
- Config YAML parse errors exit with code `2` for `chat` and `ask`.
- Missing optional config file is not an error.
- Dotenv import absence remains non-fatal.
- Approval JSON edit parse failures stay in the current item prompt.
- EOF during an approval prompt rejects all pending requests with the message
  "Rejected because approval input ended." and resumes the graph fail-closed.
- Skill command load failures return a command error and do not call the model.
- Dynamic skill command conflicts do not fail startup.

## Testing Strategy

Add or update tests for:

- per-request interrupt extraction preserving `review_configs`
- approve, reject with custom message, respond, edit, approve-all, and
  reject-all resume values
- invalid JSON edit retry behavior
- `_handle_interrupts()` resuming with the exact decisions list
- profile name validation
- `--profile` mapping to `~/.langchain-agent/profiles/<name>` when
  `AGENT_CLI_HOME` is unset
- explicit `AGENT_CLI_HOME` overriding `--profile`
- dotenv load order using fake dotenv loader
- config loading, missing config, malformed config, and `--model` precedence
- `/new` using configured default title
- skill command registration, completion, built-in conflict handling, and
  invocation message construction
- `/skills` output showing direct slash commands
- existing Phase 1 CLI tests still passing

Suggested verification command:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_paths.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_input.py \
  tests/test_agent_cli_interrupts.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_doctor.py \
  tests/test_agent_cli_logging.py \
  tests/test_agent_cli_session.py \
  tests/test_agent_cli_session_store.py \
  tests/test_agent_cli_checkpoints.py \
  tests/test_agent_cli_builders.py \
  -q
```

## Implementation Checkpoints

### Checkpoint 1: Approval UX

Implement `agent_cli.approval`, extend interrupt parsing, update REPL resume
handling, and add tests for all decision types.

### Checkpoint 2: Profile and Config

Implement profile pre-parse, path resolution, dotenv loading order, config
loading, model/default-title precedence, and doctor visibility checks.

### Checkpoint 3: Skill Slash Commands

Implement skill command discovery, completion integration, `/skills` display
updates, invocation message construction, and REPL routing.
