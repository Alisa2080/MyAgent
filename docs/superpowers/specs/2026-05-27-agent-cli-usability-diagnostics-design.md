# Agent CLI Usability and Diagnostics Design

Date: 2026-05-27

## Goal

Improve the project-native `agent_cli` so it is consistent to invoke,
diagnosable in local environments, accurately documented, and more transparent
about the active session.

This phase builds on the existing CLI rather than porting reference implementation's full CLI
stack. The current CLI already has `chat`, `ask`, `sessions`, `doctor`,
PromptSession input, slash commands, profiles, dotenv/config loading, HITL
approval handling, skill commands, background tasks, banner, and themes. The
work here tightens the user-facing surfaces around those features.

## Non-Goals

- No full-screen TUI.
- Noreference implementation model picker, provider switching framework, gateway, browser, voice,
  plugin framework, MCP hot reload, or skin engine.
- No changes to the agent runtime, model selection core, LangGraph checkpoint
  storage format, or tool execution policy.
- No network health checks and no real model calls from `doctor`.
- No background task daemon or cross-process worker recovery.

## Current Context

The relevant modules are:

- `agent_cli/main.py`: argparse setup, profile application, dotenv/config
  loading, store/checkpointer construction, command dispatch.
- `agent_cli/doctor.py`: local health checks and doctor output rendering.
- `agent_cli/session.py`: session status, history rendering, and Markdown
  export.
- `agent_cli/checkpoints.py`: SQLite checkpointer creation and message
  extraction.
- `agent_cli/repl.py`: slash command routing and session command handling.
- `agent_cli/background.py`: in-process background task metadata and runtime
  registry.
- `agent_cli/logging_config.py`: CLI log setup.
- `README.md`: public CLI usage documentation.

Important current gaps:

- `--workdir`, `--model`, and `--profile` are top-level argparse options only.
  Natural forms such as `python -m agent_cli doctor --workdir /repo` should
  work too.
- `doctor` only reports OK/FAIL and does not explicitly check
  `langgraph-checkpoint-sqlite`, API key presence, CLI storage writability, log
  writability, or background task metadata.
- README still describes the CLI as minimal/MVP and omits background tasks,
  profile/config behavior, logs, and richer approval behavior.
- Session status uses placeholder message and turn counts. History and export
  duplicate content extraction logic and export paths resolve from process cwd
  instead of CLI workdir.

## Selected Approach

Use focused module enhancements and keep the current small architecture.

The phase should not start with a broad command-system rewrite. `repl.py` has a
large command dispatch chain, but this work can be done safely by improving the
existing boundaries:

- `main.py` owns parser consistency.
- `doctor.py` owns local diagnostics.
- a new `history.py` owns checkpoint-backed message extraction, filtering,
  statistics, terminal rendering, and Markdown rendering.
- `session.py` becomes a thin session facade over `history.py`.
- README is updated to match actual behavior.

This keeps the implementation narrow while creating a cleaner base for later
command registry work.

## CLI Argument Design

Create a shared parent parser for public CLI options:

```text
--workdir <path>
--model <name>
--profile, -p <name>
```

Attach this parent parser to the top-level parser and to the `chat`, `ask`,
`sessions`, and `doctor` subparsers. The same option should be accepted before
or after the subcommand:

```bash
python -m agent_cli --profile dev doctor --workdir /repo
python -m agent_cli doctor --profile dev --workdir /repo
python -m agent_cli --model gpt-4.1 ask "hi"
python -m agent_cli ask --model gpt-4.1 "hi"
```

If an option is supplied both before and after the subcommand, argparse should
use the later parsed subcommand value. The implementation can rely on argparse's
namespace behavior as long as tests pin the expected behavior.

`apply_profile_override(argv)` must still run before path-dependent operations.
It already parses known args independently, so it should continue to see
`--profile` regardless of placement.

## Doctor Design

`HealthCheck.status` becomes one of:

```text
OK
WARN
FAIL
```

Output must be stable and script-friendly:

```text
=== CLI Health Check ===

OK    Python Version             Python 3.11.9
WARN  OPENAI_API_KEY             not set
FAIL  SQLite DB                  cannot open /path/cli.sqlite: ...
```

Exit code rules:

- `0` when checks are `OK` or `WARN`.
- `1` when any check is `FAIL`.

Checks:

- Python version is supported.
- Platform is visible.
- Required imports:
  - `prompt_toolkit`
  - `langchain`
  - `langgraph`
  - `langgraph.checkpoint.sqlite.SqliteSaver`, matching
    `agent_cli.checkpoints.create_sqlite_checkpointer`
- Workdir exists and is a directory.
- CLI home can be created and written.
- SQLite database path can be opened and written.
- Logs directory can be created and written.
- `config.yaml` is readable and semantically valid when present.
- `.env` files under CLI home and project root are visible and readable when
  present.
- `OPENAI_API_KEY` is present. Missing key is a `WARN`, because some tests,
  alternate providers, or future model configuration may not require OpenAI.
- Background task metadata is readable. Active tasks owned by dead processes or
  active rows without owner information are reported as `WARN`, not mutated by
  doctor.

`doctor` must not:

- call the model
- access the network
- create long-running background workers
- reconcile or mutate background task state except for harmless schema creation
  if opening the existing store requires it

## Session, History, and Export Design

Add `agent_cli/history.py` as the shared checkpoint-backed history layer.

Responsibilities:

- Load raw checkpoint messages for a `thread_id`.
- Normalize dict messages and LangChain message objects.
- Keep only user/human and assistant/ai messages for user-facing history.
- Convert content into display text consistently for strings, text blocks,
  dicts, and mixed content lists.
- Compute `message_count`.
- Compute `turn_count` as the count of user/human messages in the filtered
  transcript.
- Render terminal history.
- Render Markdown transcript.

`Session.status()` should use this history layer and session metadata rather
than placeholder counts. Status data should include:

- session id
- title
- created_at
- updated_at
- workdir
- model
- profile
- theme
- CLI home
- DB path
- message count
- turn count
- checkpointer availability

The `Session` object will accept optional runtime metadata fields for profile,
theme, CLI home, and DB path. `AgentCLI` will pass these fields when creating
or resuming sessions.

`/status` output should remain plain text and compact, for example:

```text
Session Status:
  Session ID: 20260527_120000_ab12cd34
  Title: Fix CLI docs
  Created: 2026-05-27 12:00
  Updated: 2026-05-27 12:14
  Workdir: /home/miku/projects/langchain
  Model: gpt-4.1
  Profile: dev
  Theme: default
  CLI Home: /home/miku/.langchain-agent/profiles/dev
  DB Path: /home/miku/.langchain-agent/profiles/dev/cli.sqlite
  Messages: 8
  Turns: 4
  Checkpointer: available
```

`/history [N]` should display the same filtered transcript. Invalid numeric
limits should return usage text instead of silently ignoring the argument.

`/export <path.md>` rules:

- Relative paths resolve against CLI workdir, not process cwd.
- Parent directories are created.
- Empty history returns a clear message and does not create a file.
- Markdown includes metadata:
  - session id
  - title
  - workdir
  - model
  - exported_at
  - message count
  - turn count
- Tool, system, middleware, and interrupt/internal messages are excluded.

## README Design

Update the Agent CLI section to match actual capabilities.

Documentation should include:

- invocation examples:
  - `python -m agent_cli`
  - `python -m agent_cli chat --resume <session_id>`
  - `python -m agent_cli ask "question"`
  - `python -m agent_cli sessions`
  - `python -m agent_cli doctor`
- public options:
  - `--workdir`
  - `--model`
  - `--profile/-p`
- state locations:
  - default CLI home
  - `AGENT_CLI_HOME`
  - SQLite database
  - history file
  - logs
- `config.yaml` example:

```yaml
display:
  markdown: render
  theme: default
model:
  name: gpt-4.1
session:
  default_title: New session
```

- slash command groups:
  - session: `/status`, `/title`, `/history`, `/export`, `/new`, `/resume`,
    `/sessions`
  - background: `/background`, `/tasks`, `/queue`, `/steer`, `/approve`,
    `/stop`
  - skills: `/skills`, `/skill <name>`, dynamic skill slash commands
  - info/utility: `/help`, `/doctor`, `/clear`, `/exit`
- approval behavior:
  - approve
  - reject
  - edit JSON args
  - respond to the agent
  - approve/reject all remaining
- troubleshooting:
  - missing checkpoint dependency
  - missing `OPENAI_API_KEY`
  - malformed config
  - SQLite or CLI home not writable
  - unknown session id
  - stale background task warnings

## Error Handling

- Parser errors keep argparse's standard exit behavior.
- Invalid profile names still return startup code `2`.
- Malformed config still returns startup code `2` for `chat` and `ask`.
- `doctor` reports malformed config as `FAIL` and exits `1`.
- Missing API key is `WARN`, not a startup failure.
- Missing checkpointer dependency is `FAIL` in doctor and remains a clear
  startup error for `chat` and `ask`.
- History extraction failures should produce a concise user-facing error for
  `/history` and `/export`, without corrupting session metadata.
- Export write failures should return `Export failed: ...` and should not create
  partial files when practical.

## Testing

Parser tests:

- top-level `--workdir`, `--model`, and `--profile` still work.
- subcommand-level `--workdir`, `--model`, and `--profile` work for `chat`,
  `ask`, `sessions`, and `doctor`.
- `ask --model x "hi"` preserves the question words.
- invalid profile names are rejected regardless of option placement.

Doctor tests:

- renderer emits stable `OK/WARN/FAIL` lines.
- WARN-only results exit `0`.
- any FAIL result exits `1`.
- missing `OPENAI_API_KEY` is WARN.
- missing checkpoint dependency is FAIL.
- CLI home/db/log writability failures are FAIL.
- config semantic errors are FAIL.
- stale background task metadata is WARN.

History/session/export tests:

- message normalization supports dicts and LangChain-like objects.
- filtered transcript excludes system/tool/internal messages.
- `message_count` and `turn_count` are computed from filtered messages.
- `/status` includes updated metadata and real counts.
- invalid `/history` limits return usage text.
- `/export relative/path.md` writes under CLI workdir.
- Markdown export contains metadata and filtered transcript.
- empty export does not create a file.

README test:

- Agent CLI docs mention profiles, config, background commands, logs, doctor,
  and richer approval decisions.

## Rollout Notes

This phase can be implemented in small commits:

1. Parser parent options and smoke tests.
2. Doctor status model and expanded checks.
3. Shared history layer and session/status/export improvements.
4. README update and documentation smoke test.

Each step should keep the existing CLI runnable with:

```bash
python -m agent_cli --help
python -m agent_cli sessions
python -m agent_cli doctor --workdir /home/miku/projects/langchain
```
