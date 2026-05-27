# Agent CLI Text UX Design

Date: 2026-05-27

## Goal

Improve the Agent CLI's everyday terminal UX by borrowing the low-risk text
handling ideas from the Hermes CLI while keeping the current project's small,
testable module structure.

This phase focuses on pure text safety and lightweight interactive commands:

- collapse large pasted text into files during editing, then expand references
  before submitting to the agent;
- strip leaked bracketed-paste and terminal response sequences from user input;
- recognize dragged or pasted local file paths and convert them into explicit
  text references;
- add `/copy [n]`, `/retry`, and `/usage` commands.

## Non-Goals

This phase does not implement image or multimodal input. Image paths are treated
as ordinary file references until the agent runtime has an explicit multimodal
message contract.

This phase does not implement `/undo`, `/branch`, `/rollback`, or checkpoint
forking. Those require a separate LangGraph checkpoint design so that state is
not corrupted by ad hoc transcript edits.

This phase does not add provider billing, rate-limit APIs, or account usage
fetching. `/usage` is intentionally local and best-effort.

## Current Context

The current CLI is already split into focused modules:

- `agent_cli/input.py` owns prompt-toolkit setup, slash completions, and path
  completion.
- `agent_cli/repl.py` owns command dispatch and message submission.
- `agent_cli/history.py` normalizes checkpoint messages for `/history`,
  `/export`, and status statistics.
- `agent_cli/session.py` exposes session metadata and transcript-derived
  counts.
- `agent_cli/checkpoints.py` contains the LangGraph checkpoint access helpers.

Hermes has mature implementations for file-drop detection, bracketed paste
cleanup, large-paste collapsing, image attachment, copy/retry/undo/branch
commands, and usage display. This project should reuse the proven behavior and
edge cases, but not copy the monolithic TUI control flow.

## Design Principles

Keep text handling as pure functions wherever possible. The REPL should decide
when to call these helpers, but parsing, sanitizing, paste-reference expansion,
and file-drop detection should be unit-testable without prompt-toolkit.

Use explicit text transformations. If user input is rewritten, the rewritten
message should make the transformation visible to the model, for example:

```text
[User referenced file: /home/miku/project/notes.md]
Summarize this.
```

Preserve user intent before convenience. Slash commands should not be collapsed
as large pastes, and text that merely resembles a path should not be rewritten
unless it resolves to a real local file.

Avoid global process state. Paste files and OSC52 behavior should be driven by
the `AgentCLI` instance's `cli_home` and session state.

## Input Safety

Add a text input safety layer in `agent_cli/input.py` or a small adjacent module
if the file becomes too broad.

The layer should provide:

- `sanitize_terminal_input(text: str) -> str`
- `detect_file_drop(text: str, *, workdir: str) -> FileDrop | None`
- `collapse_large_paste(text: str, *, cli_home: Path, counter: int) -> PasteCollapse`
- `expand_paste_references(text: str) -> str`
- helpers for parsing quoted paths, escaped spaces, `file://` URLs, `~`, and
  relative paths from the CLI workdir.

`sanitize_terminal_input` strips:

- Windows `CRLF` and old Mac `CR` line endings are normalized to `LF`;
- canonical bracketed paste wrappers: `ESC[200~` and `ESC[201~`;
- visible degraded wrappers such as `^[[200~` and `^[[201~`;
- guarded boundary forms like `[200~`, `[201~`, `00~`, and `01~`;
- CPR/DSR terminal responses such as `ESC[<row>;<col>R` and the visible
  `^[[<row>;<col>R` form.

The REPL should sanitize ordinary submitted text immediately before command
detection and message submission. This covers terminals where prompt-toolkit
does not emit a bracketed paste event.

## Large Paste Flow

When prompt-toolkit exposes a bracketed paste event, the prompt session should
collapse pasted text of at least 5 lines into a paste file unless the current
buffer starts with `/`.

Paste files are written to:

```text
<cli_home>/pastes/paste_<counter>_<HHMMSS>.txt
```

The buffer receives a short placeholder:

```text
[Pasted text #1: 12 lines -> /path/to/paste_1_153012.txt]
```

Before submitting a normal user message, the REPL expands any recognized paste
references back into the full file contents. This means the agent receives the
actual pasted content, while the terminal editing surface stays compact.

If paste-file creation fails, the CLI should fall back to inserting the
sanitized text rather than dropping user input.

The fallback text-change heuristic from Hermes can be added only if it stays
small and testable. The minimum first-phase requirement is bracketed-paste
event handling plus submit-time sanitization.

## Dragged File Paths

When a non-command input starts with a real local file path, the CLI should
rewrite it into a clear text reference.

Supported path forms:

- absolute Unix paths;
- relative paths resolved from the CLI workdir;
- `~` paths;
- `file://` URLs;
- quoted paths;
- paths with backslash-escaped spaces.

If the whole input is a path, the message becomes:

```text
[User referenced file: /absolute/path]
```

If there is trailing text, the message becomes:

```text
[User referenced file: /absolute/path]
<trailing text>
```

Images use the same text reference format in this phase. No image bytes or
multimodal payloads are sent to the agent.

## `/copy [n]`

Add `/copy [n]` to copy an assistant response using OSC52.

Behavior:

- `/copy` copies the most recent assistant response from the current CLI
  process.
- `/copy 2` copies the second most recent assistant response from the current
  CLI process.
- invalid `n` returns `Usage: /copy [N]`.
- if no assistant response is available, return a clear message.

The command should emit an OSC52 sequence to stdout when possible and return a
short confirmation. It should not shell out to platform-specific clipboard
commands.

The first implementation may track assistant replies produced during the
current process. Reading historical assistant messages from checkpoints can be
a later enhancement.

## `/retry`

Add `/retry` to resubmit the last normal user message.

Behavior:

- stores the latest attempted non-command user message before invoking the
  runner;
- `/retry` reuses that stored message and calls the same submission path;
- slash commands are not retry targets;
- if there is no retry target, return a clear message.

Keeping the last attempted message makes retry useful after transient agent
failures. The retry command should still avoid recursive behavior: `/retry`
itself is never stored as the retry target.

## `/usage`

Add a lightweight local `/usage` command.

Required fields:

- session id;
- model or display model;
- message count and turn count from `agent_cli/history.py`;
- estimated token count for the normalized transcript;
- checkpoint availability;
- checkpoint message count or serialized message byte size when available;
- most recent agent call latency.

Optional fields:

- real prompt/completion/total tokens if the runner result exposes usage
  metadata in a stable location;
- assistant responses tracked in the current process for `/copy`.

Token estimation should be deterministic and dependency-free. A simple
character or whitespace based estimate is acceptable as long as output labels it
as an estimate.

## State Additions

`AgentCLI` should keep small process-local fields:

- `last_user_message: str | None`
- `assistant_replies: list[str]`
- `last_call_elapsed_seconds: float | None`
- `last_usage_metadata: dict[str, object] | None`
- paste counter state if prompt-toolkit paste collapsing is enabled.

This state is not persisted in SQLite. Durable session history remains in the
LangGraph checkpoint and session metadata store.

## Error Handling

Input sanitation should be best-effort and never reject ordinary text.

File-drop detection should only rewrite input when a path resolves to an
existing file. Missing files leave the original input unchanged.

Paste expansion should handle missing paste files by leaving the placeholder in
place and adding a concise note only if needed. It should not raise out of
`submit_message`.

`/copy` should degrade to a message that no copy target is available or that
OSC52 is unavailable. It should not fail the REPL.

`/usage` should render partial data if checkpoint access fails.

## Testing Strategy

Unit tests should cover the pure helpers:

- bracketed-paste wrapper cleanup;
- CPR/DSR cleanup;
- line ending normalization;
- large paste collapse threshold;
- paste reference expansion;
- quoted path and escaped-space path parsing;
- `file://`, `~`, absolute, and workdir-relative path resolution;
- no rewrite for non-existing paths.

REPL tests should cover:

- normal message submit sanitizes control sequences;
- dragged file path is converted to a text reference;
- `/copy`, `/copy 2`, invalid `/copy` arguments, and no-copy-target behavior;
- `/retry` after success and after runner failure;
- `/usage` output with checkpoint unavailable and with a fake transcript;
- assistant replies and latency state are updated after successful calls.

Smoke tests should ensure `/help` lists the new commands and existing commands
still work.

## Documentation

Update README CLI docs with:

- paste behavior and paste file location;
- file path drag/drop behavior;
- `/copy [n]`;
- `/retry`;
- `/usage`;
- the explicit note that image paths are text references for now.

## Acceptance Criteria

- Large bracketed pastes are collapsed to `<cli_home>/pastes/` and expanded
  before agent submission.
- Leaked terminal control sequences do not reach the agent in normal text
  input.
- Dragged file paths at the start of input become explicit text references.
- `/copy [n]`, `/retry`, and `/usage` are available in help and tested.
- Existing Agent CLI tests continue to pass.
- No multimodal or checkpoint-forking behavior is introduced in this phase.
