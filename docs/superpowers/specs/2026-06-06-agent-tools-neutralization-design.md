# Agent Tools Neutralization Design

## Goal

Clean up `agent_tools/` so its runtime module names and documentation read as project-native LangChain code rather than imported reference code. The work is a naming and organization cleanup only: public tool behavior, tool schemas, provider behavior, and existing compatibility shims should remain stable unless a shim explicitly exposes obsolete web tool names.

## Scope

In scope:

- Rename the current externally-branded web helper package to a neutral web helper package, `agent_tools/web_toolkit/`.
- Update runtime imports, tests, README text, specs, and plans to use neutral names.
- Remove the untracked `agent_tools/web_tools.py` reference file because it imports modules that do not exist in this project and is not used by runtime code.
- Keep current project-facing tool names: `web_search` and `web_extract`.
- Keep `web_fetch` removed from public surfaces.
- Keep top-level compatibility shims such as `agent_tools.web`, `agent_tools.file_tools`, and `agent_tools.terminal_tools` unless they expose obsolete names.

Out of scope:

- Changing web tool behavior or provider semantics.
- Moving `file_toolkit`, `terminal_toolkit`, or `public` package boundaries.
- Reworking historical design decisions beyond replacing project-specific reference branding with neutral language.
- Cleaning unrelated dirty working tree items such as runtime PID files or archived CLI reference deletions.

## Current Problems

The current web migration introduced a focused implementation under an externally-branded helper package, but that package name and docstrings still tie runtime code to a reference-project identity. There is also an untracked `agent_tools/web_tools.py` file that appears to be original reference code. It imports paths such as `tools.registry`, `tools.url_safety`, and `agent.auxiliary_client`, which are not project modules. Keeping that file in `agent_tools/` makes the package look more cluttered and can mislead future work.

Historical plans and specs also contain many explicit references to that external project. Some references are useful as design history, but the user request is to fully neutralize the language. These references should become terms such as "reference implementation", "archived reference", "upstream reference", or "project-native" while preserving the technical intent of the documents.

## Proposed Structure

The web helper package becomes:

```text
agent_tools/
  public/
    web.py
  web_toolkit/
    __init__.py
    backends.py
    content.py
    safety.py
```

`agent_tools.public.web` remains the LangChain tool facade. It imports backend selection, safety, and content helpers from `agent_tools.web_toolkit`.

`agent_tools.web` remains a compatibility shim to `agent_tools.public.web`, but it should expose only `web_search` and `web_extract`.

## Documentation Neutralization

All target docs should avoid the external project name. Examples:

- External-reference web tools migration -> "web tools migration"
- External-reference-compatible -> "provider-compatible" or "project-compatible"
- External-reference files -> "archived reference files"
- provider-specific style -> "project-native" only where the behavior comparison matters, otherwise "project-native"
- Externally-branded web helper package path -> `agent_tools/web_toolkit`

File names under `docs/superpowers/specs/` and `docs/superpowers/plans/` that contain the external project name should be renamed to neutral names. Cross-references inside those files should be updated.

## Testing Strategy

Add or update tests before implementation:

- Assert imports resolve from `agent_tools.web_toolkit`.
- Assert `agent_tools.public.web` imports the neutral web helper package.
- Assert public surfaces still expose `web_search` and `web_extract`, and do not expose `web_fetch`.
- Update web migration tests to import backend/content/safety helpers from `agent_tools.web_toolkit`.

Then verify:

- `pytest tests/test_web_tools_migration.py tests/test_agent_tools_public_imports.py tests/test_public_toolmessage_results.py -q`
- `pytest tests/ -q`
- Search the targeted runtime and documentation paths for external-reference branding and the old web helper package path.

Any remaining matches must be either outside the requested cleanup scope or deliberately preserved with a neutral file path unavailable; otherwise they should be removed.

## Risks

The main risk is breaking imports from tests or runtime modules by renaming the web helper package. This is controlled by TDD import tests and full test verification.

Another risk is over-editing historical docs and changing their meaning. The cleanup should preserve technical decisions while replacing branding language with neutral reference language.

The untracked `agent_tools/web_tools.py` file should be removed only as an untracked workspace cleanup item. Since it is not tracked, this does not alter committed history, but it prevents future confusion.
