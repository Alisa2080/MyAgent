# Web Tools Migration Design

## Context

The current agent project exposes web tools through `agent_tools.public.web` and registers them in `agent_core.delegation`. The current public tool surface is:

- `web_search`: TinyFish metadata search.
- `web_fetch`: TinyFish page content fetch.

The pasted reference implementation in `agent_tools/web_tools.py` provides a stronger web toolset with provider routing, dedicated extraction, URL safety checks, base64 cleanup, and optional LLM post-processing. It is not directly compatible with this project because it imports reference implementation-specific modules such as `agent.auxiliary_client`, `tools.registry`, `tools.url_safety`, `tools.website_policy`, `tools.debug_helpers`, and managed Nous tool-gateway helpers.

The migration should replace the current TinyFish tools with a provider-compatible LangChain-native implementation. The agent-facing tools after migration are:

- `web_search`
- `web_extract`

`web_fetch` will no longer be exposed as an agent tool.

## Goals

1. Replace the current TinyFish web tool surface with provider-compatible `web_search` and `web_extract`.
2. Preserve the current project's LangChain-native public tool pattern: `@tool`, Pydantic args schemas, injected `ToolRuntime`, and `ToolMessage` results through `tool_success` / `tool_failure`.
3. Support provider-compatible provider routing across Firecrawl, Parallel, Tavily, and Exa without making any one SDK a hard import-time dependency.
4. Port the important safety behavior from reference implementation:
   - SSRF blocking for private, loopback, link-local, localhost, metadata, and non-HTTP(S) targets.
   - Embedded secret / token exfiltration blocking in URL strings, including URL-decoded forms.
   - Redirect final-URL safety checks when a backend exposes the final URL.
   - Base64 image cleanup.
   - Output size control.
5. Keep agent startup robust when optional web provider packages or credentials are missing.

## Non-Goals

1. Keep `web_fetch` as a public agent tool.
2. Port `web_crawl` in this migration.
3. Port reference implementation `tools.registry` registration.
4. Port reference implementation managed Nous tool-gateway integration as a required path.
5. Implement a full website policy / robots framework in this phase.
6. Add a hard dependency on every provider SDK.

## Public Tool Surface

### `web_search`

Args:

- `query: str`
- `limit: int = 5`, clamped to `1..100`

Behavior:

- Search only returns metadata-like results: title, URL, description/snippet, position, provider metadata where available.
- It does not fetch page bodies.
- It selects the backend using the provider selection rules below.
- It returns a `ToolMessage` named `web_search`.

Success artifact data:

```python
{
    "query": "...",
    "backend": "firecrawl|parallel|tavily|exa",
    "results": [
        {
            "title": "...",
            "url": "https://...",
            "description": "...",
            "position": 1,
            "metadata": {...}
        }
    ],
    "total": 1,
}
```

### `web_extract`

Args:

- `urls: list[str]`
- `format: Literal["markdown", "html"] = "markdown"`
- `use_llm_processing: bool = True`
- `model: str | None = None`
- `min_length: int = 5000`
- `max_chars_per_url: int = 12000`

Behavior:

- Validate all URLs before calling any backend.
- Extract readable page content for each URL.
- Clean base64 image payloads from returned content.
- Optionally summarize long content through a configured auxiliary model. If no auxiliary model is configured or the summarizer fails, fall back to bounded raw content.
- Return partial success when some URLs succeed and some fail.
- Return a `ToolMessage` named `web_extract`.

Success artifact data:

```python
{
    "backend": "firecrawl|parallel|tavily|exa",
    "results": [
        {
            "url": "https://...",
            "final_url": "https://...",
            "title": "...",
            "content": "...",
            "format": "markdown",
            "truncated": False,
            "processed": True,
            "error": None,
            "metadata": {...}
        }
    ],
    "total": 1,
}
```

Failures that affect all URLs return `tool_failure`. Per-URL failures are represented inside `results` so the model can continue using successful pages.

## Provider Selection

Backend selection should be deterministic and visible in tool artifacts.

1. Read an explicit backend from `AGENT_WEB_BACKEND` first.
2. If unset, read `WEB_BACKEND`.
3. Valid values are `parallel`, `firecrawl`, `tavily`, and `exa`.
4. If no explicit backend is configured, choose the first available backend in this order:
   - Firecrawl when `FIRECRAWL_API_KEY` or `FIRECRAWL_API_URL` is set.
   - Parallel when `PARALLEL_API_KEY` is set.
   - Tavily when `TAVILY_API_KEY` is set.
   - Exa when `EXA_API_KEY` is set.
5. If no backend is available, return a structured configuration error.

Each backend client must be lazily imported and lazily constructed. Missing provider packages must fail only when that backend is selected.

## Backend Adapters

Create a small internal adapter layer inside `agent_tools/public/web.py` or a helper module under `agent_tools/web/` if the file becomes too large.

Each adapter should implement two operations:

```python
search(query: str, limit: int) -> dict
extract(urls: list[str], format: str) -> list[dict]
```

Adapter output should be normalized before returning tool artifacts.

### Firecrawl

- Use direct Firecrawl when `FIRECRAWL_API_KEY` or `FIRECRAWL_API_URL` is configured.
- Import the Firecrawl SDK lazily.
- Support search and scrape/extract.
- Preserve compatibility with self-hosted Firecrawl via `FIRECRAWL_API_URL`.

### Parallel

- Import `parallel` lazily.
- Support metadata search and extraction.
- Keep async extraction support internally if the SDK requires it, but expose a synchronous LangChain tool function.

### Tavily

- Use `httpx` directly against Tavily APIs.
- Include API key in the request format expected by Tavily.
- Normalize search and extract responses into the project result format.

### Exa

- Import Exa lazily.
- Support neural search and content extraction.
- Normalize highlights/snippets into descriptions or metadata.

## URL Safety

Implement project-local URL safety helpers because the reference implementation helpers are not present.

`is_safe_url(url: str) -> tuple[bool, str | None]` should reject:

- Empty or malformed URLs.
- Schemes other than `http` and `https`.
- URLs with embedded credentials in userinfo.
- Hostnames resolving to or directly representing:
  - loopback addresses
  - private addresses
  - link-local addresses
  - multicast / unspecified / reserved addresses
  - `localhost`
  - common metadata service addresses such as `169.254.169.254`

DNS resolution should be bounded and defensive. If resolution fails, allow public-looking hostnames and let the backend request fail normally, but never allow literal unsafe IP addresses.

`contains_embedded_secret(url: str) -> bool` should inspect the raw and URL-decoded string for common secret material:

- `api_key=`
- `access_token=`
- `auth_token=`
- `token=`
- `secret=`
- `signature=`
- `sig=`
- OpenAI-style `sk-`
- Firecrawl `fc-`
- Tavily `tvly-`
- Exa `exa_`

Error text should be redacted with the existing project redaction helper before surfacing to the model.

## Redirect Safety

Some providers return final URL or source URL metadata. When available:

1. Validate the final URL with the same URL safety helper.
2. If unsafe, replace that document with a per-URL error result.
3. Do not include unsafe final URL content in the tool artifact.

If a provider does not expose redirect details, rely on pre-request validation and backend request behavior for this phase.

## Content Processing

### Base64 Cleanup

Port reference implementation `clean_base64_images` behavior:

- Replace `data:image/...;base64,...` payloads with `[BASE64_IMAGE_REMOVED]`.
- Remove obviously large base64-like blocks when they are embedded in HTML or Markdown.

### Output Limits

Each extracted document should be capped by `max_chars_per_url`. The default is `12000`. The tool should mark `truncated=True` when content is shortened.

### Optional LLM Processing

This project does not currently have the reference implementation's `agent.auxiliary_client`, so the first implementation should provide a small optional summarization hook:

- If `use_llm_processing=False`, skip summarization.
- If content length is below `min_length`, skip summarization.
- If `AUXILIARY_WEB_EXTRACT_API_KEY` and `AUXILIARY_WEB_EXTRACT_BASE_URL` are configured, use an OpenAI-compatible chat-completions request through `httpx`.
- Model selection order:
  1. tool `model` arg
  2. `AUXILIARY_WEB_EXTRACT_MODEL`
  3. no summarization when neither is configured
- Summarization failures must not fail extraction. They should fall back to bounded cleaned content and add a warning in metadata.

## Integration Points

Update:

- `agent_tools/public/web.py`
  - Replace TinyFish implementation.
  - Export `web_search` and `web_extract`.
- `agent_tools/web.py`
  - Keep compatibility shim to `agent_tools.public.web`.
- `agent_tools/public/__init__.py`
  - Export `web_extract` instead of `web_fetch`.
- `agent_core/delegation.py`
  - Import/register `web_search` and `web_extract`.
  - Remove `web_fetch`.
- `agent_core/tool_limits.py`
  - Replace `web_fetch` limits with `web_extract` limits.
- `agent_tools/README.md`
  - Document the new public web surface.
- Tests under `tests/`
  - Update public import expectations.
  - Update ToolMessage runtime-id tests.
  - Add provider routing tests.
  - Add URL safety tests.
  - Add base64 cleanup tests.
  - Add `web_fetch` removal assertions where appropriate.

The pasted `agent_tools/web_tools.py` can be used as a reference source during implementation, but final runtime imports should go through `agent_tools.public.web`.

## Error Handling

All public tools should return `ToolMessage`.

Use:

- `tool_success` for successful or partial-success operations.
- `tool_failure` for invalid inputs, missing backend configuration, selected backend missing credentials, selected backend missing package, and all-URL extraction failures.

Do not leak credentials in exceptions. Redact errors before returning them.

Common error codes:

- `invalid_input`
- `unsafe_url`
- `missing_configuration`
- `missing_dependency`
- `backend_error`
- `invalid_response`

## Testing Strategy

Unit tests should avoid real network calls.

Required tests:

1. `web_search` returns `ToolMessage` and preserves runtime tool call id.
2. `web_extract` returns `ToolMessage` and preserves runtime tool call id.
3. `web_fetch` is no longer exported from `agent_tools.public.web` or `agent_tools.public`.
4. Explicit backend selection honors `AGENT_WEB_BACKEND`.
5. Backend auto-selection follows the configured priority.
6. Missing credentials return a structured failure.
7. Missing optional SDK import returns a structured failure for that selected backend only.
8. Unsafe literal IP URLs are blocked.
9. URLs containing decoded secrets are blocked.
10. Redirect final URLs are rechecked when present in normalized documents.
11. Base64 image data is replaced with `[BASE64_IMAGE_REMOVED]`.
12. Extraction output is capped and marks `truncated=True`.
13. Long-content summarization falls back to raw bounded content when auxiliary configuration is absent.
14. `agent_core.delegation.BASE_TOOLS` contains `web_search` and `web_extract`, not `web_fetch`.
15. `agent_core.tool_limits` applies limits to `web_extract`.

## Migration Risks

1. Removing `web_fetch` may break tests, docs, or external imports. This is intentional for the agent-facing surface, but compatibility expectations should be updated explicitly.
2. Provider SDK APIs may differ from the pasted reference implementation assumptions. Adapters should normalize defensively and tests should mock minimal response shapes.
3. URL safety checks can over-block legitimate private-network use cases. This project should default secure; private URL support can be added later behind an explicit opt-in.
4. LLM post-processing can introduce latency or failure. It must remain optional and non-fatal.
5. Full reference implementation behavior includes managed gateway and crawl. Those are intentionally excluded from this migration to keep the replacement focused and maintainable.

## Acceptance Criteria

1. The agent has `web_search` and `web_extract` tools available through `BASE_TOOLS`.
2. The agent no longer has `web_fetch` available through `BASE_TOOLS`.
3. The public web tools follow current project `ToolMessage` conventions.
4. Search and extraction support Firecrawl, Parallel, Tavily, and Exa as optional backends.
5. The project imports successfully when none of the optional provider SDKs are installed.
6. Unsafe URLs and URL-embedded secrets are blocked before extraction.
7. Tests cover provider routing, safety checks, output cleanup, result shape, and integration registration.
