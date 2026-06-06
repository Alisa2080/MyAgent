# Web Tools Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current TinyFish `web_search` / `web_fetch` tools with provider-compatible `web_search` / `web_extract` tools for the LangChain agent.

**Architecture:** Keep the public LangChain tool boundary in `agent_tools.public.web`, but split reusable implementation into focused helper modules under `agent_tools/web_toolkit/`. Provider SDKs are optional and lazily imported. The public tools return current-project `ToolMessage` objects and use local safety, normalization, and content-processing helpers before surfacing results to the agent.

**Tech Stack:** Python, LangChain `@tool`, Pydantic schemas, `ToolMessage`, `httpx`, optional provider SDKs (`firecrawl`, `parallel`, `exa_py`/`exa`, Tavily via HTTP), pytest.

---

## File Structure

- Create: `agent_tools/web_toolkit/__init__.py`
  - Marks the internal project-native web helper package. This avoids colliding with the existing `agent_tools/web.py` public compatibility shim.
- Create: `agent_tools/web_toolkit/safety.py`
  - SSRF checks, embedded-secret URL checks, and redacted safety errors.
- Create: `agent_tools/web_toolkit/content.py`
  - Base64 image cleanup, text truncation, and optional auxiliary summarization.
- Create: `agent_tools/web_toolkit/backends.py`
  - Backend selection, lazy adapter construction, provider adapters, response normalization.
- Modify: `agent_tools/public/web.py`
  - Replace TinyFish implementation with LangChain wrappers for `web_search` and `web_extract`.
- Modify: `agent_tools/public/__init__.py`
  - Export `web_extract`; remove `web_fetch`.
- Modify: `agent_core/delegation.py`
  - Register `web_search` and `web_extract` in read-only tools; remove `web_fetch`.
- Modify: `agent_core/tool_limits.py`
  - Replace `web_fetch` limits with `web_extract` limits.
- Modify: `agent_tools/README.md`
  - Document the new public web tool surface.
- Modify: `tests/test_public_toolmessage_results.py`
  - Update ToolMessage tests for `web_extract` and removal of `web_fetch`.
- Modify: `tests/test_agent_tools_public_imports.py`
  - Update public/shim import assertions.
- Create: `tests/test_web_tools_migration.py`
  - Provider routing, safety, normalization, cleanup, and integration tests.

## Implementation Notes

- Do not use the pasted reference implementation `tools.registry` registration.
- Do not import provider SDKs at module import time.
- Do not keep `web_fetch` as a public tool or `BASE_TOOLS` entry.
- Keep `agent_tools/web.py` as the existing compatibility shim to `agent_tools.public.web`; after this migration it should expose `web_search` and `web_extract` through the shim because the module object points at public web.
- Use existing `agent_tools.shared.tool_result.tool_success` and `tool_failure`.
- Use existing `agent_tools.file_toolkit.redact.redact_sensitive_text` for error text before returning backend failures.

---

### Task 1: Lock Down Public Tool Surface Replacement

**Files:**
- Modify: `tests/test_public_toolmessage_results.py`
- Modify: `tests/test_agent_tools_public_imports.py`
- Modify: `tests/test_web_tools_migration.py`

- [ ] **Step 1: Add failing ToolMessage tests for `web_extract` and `web_fetch` removal**

Edit `tests/test_public_toolmessage_results.py`.

Replace the existing `web_fetch` runtime-id test:

```python
def test_web_fetch_preserves_runtime_tool_call_id(monkeypatch):
    import agent_tools.public.web as web

    monkeypatch.setattr(web, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

    result = web.web_fetch.func(["https://example.com"], runtime=_runtime("call-fetch"))

    _assert_tool_result(result, "web_fetch", False)
    assert result.tool_call_id == "call-fetch"
```

with:

```python
def test_web_extract_preserves_runtime_tool_call_id(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": urls[0],
                    "title": "Example",
                    "content": "hello from example",
                    "format": format,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())

    result = web.web_extract.func(["https://example.com"], runtime=_runtime("call-extract"))

    _assert_tool_result(result, "web_extract", True)
    assert result.artifact["data"]["backend"] == "fake"
    assert result.artifact["data"]["results"][0]["content"] == "hello from example"
    assert result.tool_call_id == "call-extract"
```

In `test_public_tool_entrypoints_register_runtime_for_injection`, replace `web.web_fetch` with `web.web_extract`:

```python
    tools = [
        delegation.task,
        web.web_search,
        web.web_extract,
        skills.skills_list,
        skills.skill_view,
        memory.memory_manage,
        skill_manage_impl.skill_manage,
    ]
```

Add this test near the other web tests:

```python
def test_web_fetch_is_not_public_tool():
    import agent_tools.public.web as web

    assert not hasattr(web, "web_fetch")
```

- [ ] **Step 2: Update public import tests to expect `web_extract`**

Edit `tests/test_agent_tools_public_imports.py`.

Replace lines importing and asserting `web_fetch`:

```python
    from agent_tools.public.web import web_fetch as public_web_fetch
    from agent_tools.public.web import web_search as public_web_search
    from agent_tools.web import web_fetch, web_search

    assert public_web_search is web_search
    assert public_web_fetch is web_fetch
```

with:

```python
    from agent_tools.public.web import web_extract as public_web_extract
    from agent_tools.public.web import web_search as public_web_search
    from agent_tools.web import web_extract, web_search

    assert public_web_search is web_search
    assert public_web_extract is web_extract
```

Add this test after `test_public_web_memory_and_skills_exports_existing_tool_objects`:

```python
def test_web_fetch_removed_from_public_surfaces():
    import agent_tools.public as public
    import agent_tools.public.web as public_web

    assert "web_fetch" not in public.__all__
    assert not hasattr(public_web, "web_fetch")
    assert not hasattr(public, "web_fetch")
```

- [ ] **Step 3: Add integration surface tests**

Create `tests/test_web_tools_migration.py` with:

```python
from types import SimpleNamespace


def _runtime(tool_call_id: str = "call-web"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="web-migration-thread"),
        tool_call_id=tool_call_id,
    )


def test_base_tools_include_web_extract_not_web_fetch():
    import agent_core.delegation as delegation

    names = {tool.name for tool in delegation.BASE_TOOLS}

    assert "web_search" in names
    assert "web_extract" in names
    assert "web_fetch" not in names


def test_tool_limits_include_web_extract_not_web_fetch():
    from agent_core.tool_limits import build_tool_call_limit_middleware

    middleware = build_tool_call_limit_middleware()
    names = {item.tool_name for item in middleware}

    assert "web_search" in names
    assert "web_extract" in names
    assert "web_fetch" not in names
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
pytest tests/test_public_toolmessage_results.py::test_web_extract_preserves_runtime_tool_call_id \
  tests/test_public_toolmessage_results.py::test_web_fetch_is_not_public_tool \
  tests/test_agent_tools_public_imports.py::test_public_web_memory_and_skills_exports_existing_tool_objects \
  tests/test_agent_tools_public_imports.py::test_web_fetch_removed_from_public_surfaces \
  tests/test_web_tools_migration.py::test_base_tools_include_web_extract_not_web_fetch \
  tests/test_web_tools_migration.py::test_tool_limits_include_web_extract_not_web_fetch -v
```

Expected: FAIL because `web_extract` does not exist, `web_fetch` is still exported, and `BASE_TOOLS` / tool limits still contain `web_fetch`.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py tests/test_web_tools_migration.py
git commit -m "test: define reference implementation web tool public surface"
```

---

### Task 2: Implement URL Safety and Content Cleanup Helpers

**Files:**
- Create: `agent_tools/web_toolkit/__init__.py`
- Create: `agent_tools/web_toolkit/safety.py`
- Create: `agent_tools/web_toolkit/content.py`
- Modify: `tests/test_web_tools_migration.py`

- [ ] **Step 1: Add failing safety and cleanup tests**

Append to `tests/test_web_tools_migration.py`:

```python
def test_is_safe_url_blocks_private_and_local_targets():
    from agent_tools.web_toolkit.safety import is_safe_url

    unsafe_urls = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://10.0.0.1/admin",
        "http://172.16.0.1/admin",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ]

    for url in unsafe_urls:
        ok, reason = is_safe_url(url)
        assert ok is False, url
        assert reason


def test_is_safe_url_allows_public_https_hostname(monkeypatch):
    from agent_tools.web_toolkit import safety

    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args, **kwargs: [])

    ok, reason = safety.is_safe_url("https://example.com/docs")

    assert ok is True
    assert reason is None


def test_contains_embedded_secret_detects_raw_and_encoded_values():
    from agent_tools.web_toolkit.safety import contains_embedded_secret

    assert contains_embedded_secret("https://example.com/?api_key=abc")
    assert contains_embedded_secret("https://example.com/?q=sk-abc123")
    assert contains_embedded_secret("https://example.com/?q=%73%6b-abc123")
    assert not contains_embedded_secret("https://example.com/?q=public")


def test_clean_base64_images_replaces_data_uri_payloads():
    from agent_tools.web_toolkit.content import clean_base64_images

    text = "before data:image/png;base64," + ("A" * 200) + " after"

    cleaned = clean_base64_images(text)

    assert "[BASE64_IMAGE_REMOVED]" in cleaned
    assert "A" * 80 not in cleaned
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_is_safe_url_blocks_private_and_local_targets \
  tests/test_web_tools_migration.py::test_is_safe_url_allows_public_https_hostname \
  tests/test_web_tools_migration.py::test_contains_embedded_secret_detects_raw_and_encoded_values \
  tests/test_web_tools_migration.py::test_clean_base64_images_replaces_data_uri_payloads -v
```

Expected: FAIL because `agent_tools.web_toolkit.safety` and `agent_tools.web_toolkit.content` do not exist.

- [ ] **Step 3: Create helper package**

Create `agent_tools/web_toolkit/__init__.py`:

```python
"""Internal helpers for LangChain web tools."""
```

- [ ] **Step 4: Implement URL safety**

Create `agent_tools/web_toolkit/safety.py`:

```python
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import unquote, urlparse


_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}
_METADATA_HOSTS = {"169.254.169.254"}
_SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:^|[?&#;])(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|signature|sig)=",
        r"sk-[A-Za-z0-9_-]{6,}",
        r"fc-[A-Za-z0-9_-]{6,}",
        r"tvly-[A-Za-z0-9_-]{6,}",
        r"exa_[A-Za-z0-9_-]{6,}",
    )
]


def _ip_is_unsafe(ip: ipaddress._BaseAddress) -> bool:
    return any(
        (
            ip.is_loopback,
            ip.is_private,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_unspecified,
            ip.is_reserved,
        )
    )


def contains_embedded_secret(url: str) -> bool:
    raw = str(url or "")
    decoded = unquote(raw)
    return any(pattern.search(raw) or pattern.search(decoded) for pattern in _SECRET_PATTERNS)


def _literal_ip(hostname: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return None


def is_safe_url(url: str) -> tuple[bool, str | None]:
    value = str(url or "").strip()
    if not value:
        return False, "URL is required"
    if contains_embedded_secret(value):
        return False, "URL contains embedded credential material"

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False, "URL scheme must be http or https"
    if not parsed.hostname:
        return False, "URL hostname is required"
    if parsed.username or parsed.password:
        return False, "URL must not include embedded credentials"

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in _LOCAL_HOSTNAMES:
        return False, "localhost URLs are not allowed"
    if hostname in _METADATA_HOSTS:
        return False, "metadata service URLs are not allowed"

    literal = _literal_ip(hostname)
    if literal is not None:
        if _ip_is_unsafe(literal):
            return False, "private or local IP URLs are not allowed"
        return True, None

    try:
        infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return True, None

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _ip_is_unsafe(ip):
            return False, "hostname resolves to a private or local IP"
    return True, None
```

- [ ] **Step 5: Implement content cleanup**

Create `agent_tools/web_toolkit/content.py`:

```python
from __future__ import annotations

import re
from typing import Any

import httpx


DEFAULT_MAX_CHARS_PER_URL = 12000
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000
_BASE64_DATA_URI_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{80,}",
    re.IGNORECASE,
)
_LARGE_BASE64_BLOCK_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{800,}(?![A-Za-z0-9+/=])")


def clean_base64_images(text: str) -> str:
    value = "" if text is None else str(text)
    value = _BASE64_DATA_URI_RE.sub("[BASE64_IMAGE_REMOVED]", value)
    return _LARGE_BASE64_BLOCK_RE.sub("[BASE64_IMAGE_REMOVED]", value)


def bound_content(content: str, max_chars: int) -> tuple[str, bool]:
    value = "" if content is None else str(content)
    limit = max(int(max_chars or 0), 0)
    if limit <= 0:
        return "", bool(value)
    if len(value) <= limit:
        return value, False
    marker = "\n\n[Content truncated for context management.]"
    if limit <= len(marker):
        return marker[:limit], True
    return value[: limit - len(marker)].rstrip() + marker, True


def should_summarize(content: str, *, use_llm_processing: bool, min_length: int, model: str | None) -> bool:
    if not use_llm_processing:
        return False
    if len(content or "") < max(int(min_length or 0), 0):
        return False
    return bool(model)


def summarize_with_auxiliary(
    content: str,
    *,
    url: str,
    title: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float = 60.0,
) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    prompt = (
        "Extract the useful article or page content as concise markdown. "
        "Preserve key facts, names, dates, numbers, and source-specific claims. "
        "Remove navigation, boilerplate, ads, and repeated junk.\n\n"
        f"Title: {title or '(untitled)'}\n"
        f"URL: {url}\n\n"
        f"{content}"
    )
    response = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return str(payload["choices"][0]["message"]["content"]).strip()
```

- [ ] **Step 6: Run helper tests**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_is_safe_url_blocks_private_and_local_targets \
  tests/test_web_tools_migration.py::test_is_safe_url_allows_public_https_hostname \
  tests/test_web_tools_migration.py::test_contains_embedded_secret_detects_raw_and_encoded_values \
  tests/test_web_tools_migration.py::test_clean_base64_images_replaces_data_uri_payloads -v
```

Expected: PASS.

- [ ] **Step 7: Commit helpers**

```bash
git add agent_tools/web_toolkit/__init__.py agent_tools/web_toolkit/safety.py agent_tools/web_toolkit/content.py tests/test_web_tools_migration.py
git commit -m "feat: add web safety and content helpers"
```

---

### Task 3: Implement Backend Selection and Provider Adapters

**Files:**
- Create: `agent_tools/web_toolkit/backends.py`
- Modify: `tests/test_web_tools_migration.py`

- [ ] **Step 1: Add failing backend routing and normalization tests**

Append to `tests/test_web_tools_migration.py`:

```python
def test_explicit_backend_selection_honors_agent_web_backend(monkeypatch):
    from agent_tools.web_toolkit import backends

    monkeypatch.setenv("AGENT_WEB_BACKEND", "parallel")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-key")
    monkeypatch.delenv("WEB_BACKEND", raising=False)

    selected = backends.select_backend_name()

    assert selected == "parallel"


def test_backend_auto_selection_priority(monkeypatch):
    from agent_tools.web_toolkit import backends

    monkeypatch.delenv("AGENT_WEB_BACKEND", raising=False)
    monkeypatch.delenv("WEB_BACKEND", raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-key")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    assert backends.select_backend_name() == "firecrawl"

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert backends.select_backend_name() == "parallel"

    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    assert backends.select_backend_name() == "tavily"

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert backends.select_backend_name() == "exa"


def test_missing_backend_configuration_raises_structured_error(monkeypatch):
    from agent_tools.web_toolkit import backends

    for key in (
        "AGENT_WEB_BACKEND",
        "WEB_BACKEND",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "PARALLEL_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    try:
        backends.get_backend()
    except backends.BackendConfigurationError as exc:
        assert exc.code == "missing_configuration"
        assert "No web backend configured" in str(exc)
    else:
        raise AssertionError("expected BackendConfigurationError")


def test_missing_optional_sdk_fails_only_selected_backend(monkeypatch):
    from agent_tools.web_toolkit import backends

    monkeypatch.setenv("AGENT_WEB_BACKEND", "firecrawl")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-key")
    monkeypatch.setattr(backends, "_import_module", lambda name: (_ for _ in ()).throw(ImportError(name)))

    try:
        backends.get_backend().search("langchain", 1)
    except backends.BackendConfigurationError as exc:
        assert exc.code == "missing_dependency"
        assert "firecrawl" in str(exc).lower()
    else:
        raise AssertionError("expected BackendConfigurationError")


def test_normalize_search_result_shape():
    from agent_tools.web_toolkit.backends import normalize_search_results

    results = normalize_search_results(
        "exa",
        [
            {
                "title": "Example",
                "url": "https://example.com",
                "text": "Snippet",
                "score": 0.9,
            }
        ],
        limit=5,
    )

    assert results == [
        {
            "title": "Example",
            "url": "https://example.com",
            "description": "Snippet",
            "position": 1,
            "metadata": {"provider": "exa", "score": 0.9},
        }
    ]
```

- [ ] **Step 2: Run routing tests to verify they fail**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_explicit_backend_selection_honors_agent_web_backend \
  tests/test_web_tools_migration.py::test_backend_auto_selection_priority \
  tests/test_web_tools_migration.py::test_missing_backend_configuration_raises_structured_error \
  tests/test_web_tools_migration.py::test_missing_optional_sdk_fails_only_selected_backend \
  tests/test_web_tools_migration.py::test_normalize_search_result_shape -v
```

Expected: FAIL because `agent_tools.web_toolkit.backends` does not exist.

- [ ] **Step 3: Implement backend adapter module**

Create `agent_tools/web_toolkit/backends.py`:

```python
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


VALID_BACKENDS = {"parallel", "firecrawl", "tavily", "exa"}
AUTO_BACKEND_ENV_ORDER = (
    ("firecrawl", ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL")),
    ("parallel", ("PARALLEL_API_KEY",)),
    ("tavily", ("TAVILY_API_KEY",)),
    ("exa", ("EXA_API_KEY",)),
)


class BackendConfigurationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "missing_configuration"):
        super().__init__(message)
        self.code = code


class WebBackend(Protocol):
    name: str

    def search(self, query: str, limit: int) -> dict[str, Any]:
        ...

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        ...


def _import_module(name: str) -> Any:
    return importlib.import_module(name)


def _obj_get(obj: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _configured(name: str) -> bool:
    return bool(os.getenv(name))


def select_backend_name() -> str:
    explicit = (os.getenv("AGENT_WEB_BACKEND") or os.getenv("WEB_BACKEND") or "").strip().lower()
    if explicit:
        if explicit not in VALID_BACKENDS:
            raise BackendConfigurationError(
                f"Unsupported web backend '{explicit}'. Valid values: exa, firecrawl, parallel, tavily.",
                code="invalid_input",
            )
        return explicit

    for backend_name, env_names in AUTO_BACKEND_ENV_ORDER:
        if any(_configured(env_name) for env_name in env_names):
            return backend_name

    raise BackendConfigurationError(
        "No web backend configured. Set AGENT_WEB_BACKEND or credentials for Firecrawl, Parallel, Tavily, or Exa.",
        code="missing_configuration",
    )


def normalize_search_results(backend: str, raw_results: list[Any], *, limit: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for position, item in enumerate(raw_results[: max(limit, 0)], start=1):
        url = str(_obj_get(item, "url", "link", default="") or "")
        title = str(_obj_get(item, "title", "name", default="") or "(untitled)")
        description = str(
            _obj_get(item, "description", "snippet", "text", "summary", "content", default="") or ""
        )
        metadata: dict[str, Any] = {"provider": backend}
        for key in ("score", "published_date", "source", "id"):
            value = _obj_get(item, key, default=None)
            if value is not None:
                metadata[key] = value
        normalized.append(
            {
                "title": title,
                "url": url,
                "description": description,
                "position": position,
                "metadata": metadata,
            }
        )
    return normalized


def normalize_extract_result(backend: str, item: Any, *, requested_format: str) -> dict[str, Any]:
    url = str(_obj_get(item, "url", "source_url", default="") or "")
    final_url = str(_obj_get(item, "final_url", "sourceURL", "source_url", "url", default=url) or url)
    title = str(_obj_get(item, "title", default="") or "")
    markdown = _obj_get(item, "markdown", "text", "content", "summary", default="")
    html = _obj_get(item, "html", default="")
    content = html if requested_format == "html" and html else markdown
    metadata = _obj_get(item, "metadata", default={}) or {}
    if not isinstance(metadata, dict):
        metadata = {"raw_metadata": metadata}
    metadata.setdefault("provider", backend)
    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "content": str(content or ""),
        "format": requested_format,
        "truncated": False,
        "processed": False,
        "error": None,
        "metadata": metadata,
    }


@dataclass
class FirecrawlBackend:
    api_key: str | None
    api_url: str | None
    name: str = "firecrawl"

    def _client(self) -> Any:
        try:
            module = _import_module("firecrawl")
        except ImportError as exc:
            raise BackendConfigurationError("Firecrawl backend selected but firecrawl SDK is not installed.", code="missing_dependency") from exc
        client_class = getattr(module, "FirecrawlApp", None) or getattr(module, "Firecrawl", None)
        if client_class is None:
            raise BackendConfigurationError("Installed firecrawl package does not expose FirecrawlApp.", code="missing_dependency")
        kwargs = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_url:
            kwargs["api_url"] = self.api_url
        return client_class(**kwargs)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        response = self._client().search(query=query, limit=limit)
        raw = _obj_get(response, "data", "results", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        client = self._client()
        docs = []
        for url in urls:
            response = client.scrape_url(url, formats=[format])
            raw = _obj_get(response, "data", default=response)
            doc = normalize_extract_result(self.name, raw, requested_format=format)
            doc["url"] = doc["url"] or url
            docs.append(doc)
        return docs


@dataclass
class ParallelBackend:
    api_key: str
    name: str = "parallel"

    def _client(self) -> Any:
        try:
            module = _import_module("parallel")
        except ImportError as exc:
            raise BackendConfigurationError("Parallel backend selected but parallel SDK is not installed.", code="missing_dependency") from exc
        client_class = getattr(module, "Parallel", None) or getattr(module, "Client", None)
        if client_class is None:
            raise BackendConfigurationError("Installed parallel package does not expose a client class.", code="missing_dependency")
        return client_class(api_key=self.api_key)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        client = self._client()
        method = getattr(client, "search", None)
        if method is None:
            raise BackendConfigurationError("Parallel client does not expose search.", code="missing_dependency")
        response = method(query=query, limit=limit)
        raw = _obj_get(response, "results", "data", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        client = self._client()
        method = getattr(client, "extract", None) or getattr(client, "scrape", None)
        if method is None:
            raise BackendConfigurationError("Parallel client does not expose extract.", code="missing_dependency")
        response = method(urls=urls, format=format)
        raw = _obj_get(response, "results", "data", default=response)
        return [normalize_extract_result(self.name, item, requested_format=format) for item in _as_list(raw)]


@dataclass
class TavilyBackend:
    api_key: str
    name: str = "tavily"

    def search(self, query: str, limit: int) -> dict[str, Any]:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": self.api_key, "query": query, "max_results": limit},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("results", [])
        return {"backend": self.name, "results": normalize_search_results(self.name, raw, limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        response = httpx.post(
            "https://api.tavily.com/extract",
            json={"api_key": self.api_key, "urls": urls, "extract_depth": "advanced", "format": format},
            timeout=60.0,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("results", [])
        return [normalize_extract_result(self.name, item, requested_format=format) for item in raw]


@dataclass
class ExaBackend:
    api_key: str
    name: str = "exa"

    def _client(self) -> Any:
        try:
            module = _import_module("exa_py")
        except ImportError:
            try:
                module = _import_module("exa")
            except ImportError as exc:
                raise BackendConfigurationError("Exa backend selected but Exa SDK is not installed.", code="missing_dependency") from exc
        client_class = getattr(module, "Exa", None)
        if client_class is None:
            raise BackendConfigurationError("Installed Exa package does not expose Exa.", code="missing_dependency")
        return client_class(api_key=self.api_key)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        response = self._client().search(query, num_results=limit)
        raw = _obj_get(response, "results", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        response = self._client().get_contents(urls, text=format == "markdown", highlights=True)
        raw = _obj_get(response, "results", default=response)
        return [normalize_extract_result(self.name, item, requested_format=format) for item in _as_list(raw)]


def get_backend() -> WebBackend:
    name = select_backend_name()
    if name == "firecrawl":
        api_key = os.getenv("FIRECRAWL_API_KEY")
        api_url = os.getenv("FIRECRAWL_API_URL")
        if not api_key and not api_url:
            raise BackendConfigurationError("Firecrawl backend requires FIRECRAWL_API_KEY or FIRECRAWL_API_URL.", code="missing_configuration")
        return FirecrawlBackend(api_key=api_key, api_url=api_url)
    if name == "parallel":
        api_key = os.getenv("PARALLEL_API_KEY")
        if not api_key:
            raise BackendConfigurationError("Parallel backend requires PARALLEL_API_KEY.", code="missing_configuration")
        return ParallelBackend(api_key=api_key)
    if name == "tavily":
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise BackendConfigurationError("Tavily backend requires TAVILY_API_KEY.", code="missing_configuration")
        return TavilyBackend(api_key=api_key)
    if name == "exa":
        api_key = os.getenv("EXA_API_KEY")
        if not api_key:
            raise BackendConfigurationError("Exa backend requires EXA_API_KEY.", code="missing_configuration")
        return ExaBackend(api_key=api_key)
    raise BackendConfigurationError(f"Unsupported web backend '{name}'.", code="invalid_input")
```

- [ ] **Step 4: Run routing tests**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_explicit_backend_selection_honors_agent_web_backend \
  tests/test_web_tools_migration.py::test_backend_auto_selection_priority \
  tests/test_web_tools_migration.py::test_missing_backend_configuration_raises_structured_error \
  tests/test_web_tools_migration.py::test_missing_optional_sdk_fails_only_selected_backend \
  tests/test_web_tools_migration.py::test_normalize_search_result_shape -v
```

Expected: PASS.

- [ ] **Step 5: Commit backend layer**

```bash
git add agent_tools/web_toolkit/backends.py tests/test_web_tools_migration.py
git commit -m "feat: add web backend routing adapters"
```

---

### Task 4: Implement `web_search` and `web_extract` Public Tools

**Files:**
- Modify: `agent_tools/public/web.py`
- Modify: `tests/test_web_tools_migration.py`
- Modify: `tests/test_public_toolmessage_results.py`

- [ ] **Step 1: Add failing public tool behavior tests**

Append to `tests/test_web_tools_migration.py`:

```python
def test_web_search_clamps_limit_and_returns_backend_artifact(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def search(self, query, limit):
            assert limit == 100
            return {
                "backend": self.name,
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.com",
                        "description": "Snippet",
                        "position": 1,
                        "metadata": {"provider": self.name},
                    }
                ],
            }

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())

    result = web.web_search.func("langchain", limit=1000, runtime=_runtime("call-search"))

    assert result.artifact["ok"] is True
    assert result.artifact["data"]["query"] == "langchain"
    assert result.artifact["data"]["backend"] == "fake"
    assert result.artifact["data"]["total"] == 1
    assert result.tool_call_id == "call-search"


def test_web_extract_blocks_unsafe_url_before_backend(monkeypatch):
    import agent_tools.public.web as web

    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("backend should not be called")

    monkeypatch.setattr(web, "get_backend", fail_if_called)

    result = web.web_extract.func(["http://127.0.0.1:8000"], runtime=_runtime("call-unsafe"))

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsafe_url"
    assert called is False


def test_web_extract_rechecks_unsafe_final_url(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": "http://127.0.0.1/admin",
                    "title": "Unsafe redirect",
                    "content": "private data",
                    "format": format,
                    "truncated": False,
                    "processed": False,
                    "error": None,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())

    result = web.web_extract.func(["https://example.com"], runtime=_runtime("call-redirect"))

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsafe_url"
    assert result.artifact["data"]["results"][0]["content"] == ""
    assert "private data" not in str(result.artifact)


def test_web_extract_cleans_base64_and_caps_output(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": urls[0],
                    "title": "Large",
                    "content": "prefix data:image/png;base64," + ("A" * 200) + " " + ("B" * 100),
                    "format": format,
                    "truncated": False,
                    "processed": False,
                    "error": None,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())

    result = web.web_extract.func(
        ["https://example.com"],
        use_llm_processing=False,
        max_chars_per_url=60,
        runtime=_runtime("call-clean"),
    )

    doc = result.artifact["data"]["results"][0]
    assert "[BASE64_IMAGE_REMOVED]" in doc["content"]
    assert "A" * 80 not in doc["content"]
    assert doc["truncated"] is True
```

- [ ] **Step 2: Run public tool tests to verify they fail**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_web_search_clamps_limit_and_returns_backend_artifact \
  tests/test_web_tools_migration.py::test_web_extract_blocks_unsafe_url_before_backend \
  tests/test_web_tools_migration.py::test_web_extract_rechecks_unsafe_final_url \
  tests/test_web_tools_migration.py::test_web_extract_cleans_base64_and_caps_output \
  tests/test_public_toolmessage_results.py::test_web_extract_preserves_runtime_tool_call_id -v
```

Expected: FAIL because `agent_tools.public.web` still contains TinyFish `web_fetch` and no `web_extract`.

- [ ] **Step 3: Replace `agent_tools/public/web.py` implementation**

Replace the file contents with:

```python
from __future__ import annotations

import os
from typing import Literal

import dotenv
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_tools.file_toolkit.redact import redact_sensitive_text
from agent_tools.shared.tool_result import tool_failure, tool_success
from agent_tools.web_toolkit.backends import BackendConfigurationError, get_backend
from agent_tools.web_toolkit.content import (
    DEFAULT_MAX_CHARS_PER_URL,
    DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    bound_content,
    clean_base64_images,
    should_summarize,
    summarize_with_auxiliary,
)
from agent_tools.web_toolkit.safety import is_safe_url


dotenv.load_dotenv()


class SearchInput(BaseModel):
    query: str = Field(description="The search query.")
    limit: int = Field(default=5, description="Maximum number of search results to return.")


class ExtractInput(BaseModel):
    urls: list[str] = Field(description="A list of URLs to extract.")
    format: Literal["markdown", "html"] = Field(default="markdown", description="Output format to request.")
    use_llm_processing: bool = Field(default=True, description="Whether to use optional LLM post-processing.")
    model: str | None = Field(default=None, description="Optional auxiliary model name.")
    min_length: int = Field(default=DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION, description="Minimum content length for summarization.")
    max_chars_per_url: int = Field(default=DEFAULT_MAX_CHARS_PER_URL, description="Maximum characters to keep per URL.")


def _redact(message: object) -> str:
    return redact_sensitive_text(str(message))


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit or 5), 100))


def _validate_urls(urls: list[str]) -> tuple[bool, str | None]:
    if not urls:
        return False, "urls must contain at least one URL"
    for url in urls:
        ok, reason = is_safe_url(url)
        if not ok:
            return False, f"Unsafe URL blocked: {reason}"
    return True, None


def _summarization_config(model: str | None) -> tuple[str | None, str | None, str | None]:
    selected_model = model or os.getenv("AUXILIARY_WEB_EXTRACT_MODEL")
    base_url = os.getenv("AUXILIARY_WEB_EXTRACT_BASE_URL")
    api_key = os.getenv("AUXILIARY_WEB_EXTRACT_API_KEY")
    if not selected_model or not base_url or not api_key:
        return None, None, None
    return selected_model, base_url, api_key


def _process_document(
    doc: dict,
    *,
    format: str,
    use_llm_processing: bool,
    model: str | None,
    min_length: int,
    max_chars_per_url: int,
) -> dict:
    result = {
        "url": str(doc.get("url") or ""),
        "final_url": str(doc.get("final_url") or doc.get("url") or ""),
        "title": str(doc.get("title") or ""),
        "content": str(doc.get("content") or ""),
        "format": format,
        "truncated": bool(doc.get("truncated", False)),
        "processed": bool(doc.get("processed", False)),
        "error": doc.get("error"),
        "metadata": dict(doc.get("metadata") or {}),
    }
    if result["error"]:
        result["content"] = ""
        return result

    final_url = result["final_url"] or result["url"]
    ok, reason = is_safe_url(final_url)
    if not ok:
        return {
            **result,
            "content": "",
            "truncated": False,
            "processed": False,
            "error": {"code": "unsafe_url", "message": f"Unsafe final URL blocked: {reason}"},
        }

    cleaned = clean_base64_images(result["content"])
    selected_model, base_url, api_key = _summarization_config(model)
    if should_summarize(cleaned, use_llm_processing=use_llm_processing, min_length=min_length, model=selected_model):
        try:
            cleaned = summarize_with_auxiliary(
                cleaned,
                url=result["url"],
                title=result["title"],
                model=selected_model or "",
                base_url=base_url or "",
                api_key=api_key or "",
            )
            result["processed"] = True
        except Exception as exc:
            result["metadata"].setdefault("warnings", []).append(f"auxiliary summarization failed: {_redact(exc)}")

    bounded, truncated = bound_content(cleaned, max_chars_per_url)
    result["content"] = bounded
    result["truncated"] = bool(truncated or result["truncated"])
    return result


@tool("web_search", args_schema=SearchInput)
def web_search(
    query: str,
    limit: int = 5,
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Search the web and return metadata results. Returns JSON: status, message, data."""
    if not str(query or "").strip():
        return tool_failure("web_search", "query is required", code="invalid_input", runtime=runtime)
    clamped_limit = _clamp_limit(limit)
    try:
        backend = get_backend()
        response = backend.search(str(query), clamped_limit)
    except BackendConfigurationError as exc:
        return tool_failure("web_search", _redact(exc), code=exc.code, runtime=runtime)
    except Exception as exc:
        return tool_failure("web_search", f"web_search failed: {_redact(exc)}", code="backend_error", runtime=runtime)

    backend_name = str(response.get("backend") or getattr(backend, "name", "unknown"))
    results = list(response.get("results") or [])[:clamped_limit]
    return tool_success(
        "web_search",
        data={"query": query, "backend": backend_name, "results": results, "total": len(results)},
        message="Search completed.",
        content=f"Search completed with {len(results)} result(s) using {backend_name}.",
        runtime=runtime,
    )


@tool("web_extract", args_schema=ExtractInput)
def web_extract(
    urls: list[str],
    format: Literal["markdown", "html"] = "markdown",
    use_llm_processing: bool = True,
    model: str | None = None,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    max_chars_per_url: int = DEFAULT_MAX_CHARS_PER_URL,
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Extract readable page content. Returns JSON: status, message, data."""
    ok, reason = _validate_urls(urls)
    if not ok:
        return tool_failure("web_extract", reason or "invalid URLs", code="unsafe_url", runtime=runtime)

    try:
        backend = get_backend()
        raw_docs = backend.extract(urls, format)
    except BackendConfigurationError as exc:
        return tool_failure("web_extract", _redact(exc), code=exc.code, runtime=runtime)
    except Exception as exc:
        return tool_failure("web_extract", f"web_extract failed: {_redact(exc)}", code="backend_error", runtime=runtime)

    backend_name = getattr(backend, "name", "unknown")
    results = [
        _process_document(
            doc,
            format=format,
            use_llm_processing=use_llm_processing,
            model=model,
            min_length=min_length,
            max_chars_per_url=max_chars_per_url,
        )
        for doc in raw_docs
    ]
    if not results:
        return tool_failure(
            "web_extract",
            "No extract results returned.",
            code="invalid_response",
            data={"backend": backend_name, "results": [], "total": 0},
            runtime=runtime,
        )

    successes = [doc for doc in results if not doc.get("error")]
    data = {"backend": backend_name, "results": results, "total": len(results)}
    if not successes:
        first_error = results[0].get("error") or {"code": "backend_error", "message": "All URLs failed."}
        return tool_failure(
            "web_extract",
            str(first_error.get("message") or "All URLs failed."),
            code=str(first_error.get("code") or "backend_error"),
            data=data,
            runtime=runtime,
        )

    return tool_success(
        "web_extract",
        data=data,
        message="Extraction completed.",
        content=f"Extracted {len(successes)} of {len(results)} URL(s) using {backend_name}.",
        runtime=runtime,
    )
```

- [ ] **Step 4: Run public tool tests**

Run:

```bash
pytest tests/test_public_toolmessage_results.py::test_web_search_preserves_runtime_tool_call_id \
  tests/test_public_toolmessage_results.py::test_web_extract_preserves_runtime_tool_call_id \
  tests/test_web_tools_migration.py::test_web_search_clamps_limit_and_returns_backend_artifact \
  tests/test_web_tools_migration.py::test_web_extract_blocks_unsafe_url_before_backend \
  tests/test_web_tools_migration.py::test_web_extract_rechecks_unsafe_final_url \
  tests/test_web_tools_migration.py::test_web_extract_cleans_base64_and_caps_output -v
```

Expected: PASS.

- [ ] **Step 5: Commit public tools**

```bash
git add agent_tools/public/web.py tests/test_public_toolmessage_results.py tests/test_web_tools_migration.py
git commit -m "feat: expose project-native web tools"
```

---

### Task 5: Update Exports, Delegation, Limits, and Docs

**Files:**
- Modify: `agent_tools/public/__init__.py`
- Modify: `agent_core/delegation.py`
- Modify: `agent_core/tool_limits.py`
- Modify: `agent_tools/README.md`
- Modify: `tests/test_agent_tools_public_imports.py`
- Modify: `tests/test_public_toolmessage_results.py`
- Modify: `tests/test_web_tools_migration.py`

- [ ] **Step 1: Update public package exports**

In `agent_tools/public/__init__.py`, replace:

```python
from agent_tools.public.web import web_fetch, web_search
```

with:

```python
from agent_tools.public.web import web_extract, web_search
```

In `__all__`, replace:

```python
    "web_fetch",
```

with:

```python
    "web_extract",
```

- [ ] **Step 2: Update delegation tool registration**

In `agent_core/delegation.py`, replace:

```python
from agent_tools.public.web import web_fetch, web_search
```

with:

```python
from agent_tools.public.web import web_extract, web_search
```

In `READ_ONLY_TOOLS`, replace:

```python
    web_fetch,
```

with:

```python
    web_extract,
```

- [ ] **Step 3: Update tool limits**

In `agent_core/tool_limits.py`, replace:

```python
WEB_FETCH_RUN_LIMIT = 5
WEB_FETCH_THREAD_LIMIT = 12
```

with:

```python
WEB_EXTRACT_RUN_LIMIT = 5
WEB_EXTRACT_THREAD_LIMIT = 12
```

Replace the middleware entry:

```python
        ToolCallLimitMiddleware(
            tool_name="web_fetch",
            run_limit=WEB_FETCH_RUN_LIMIT,
            thread_limit=WEB_FETCH_THREAD_LIMIT,
        ),
```

with:

```python
        ToolCallLimitMiddleware(
            tool_name="web_extract",
            run_limit=WEB_EXTRACT_RUN_LIMIT,
            thread_limit=WEB_EXTRACT_THREAD_LIMIT,
        ),
```

- [ ] **Step 4: Update README web surface**

In `agent_tools/README.md`, replace the web bullet:

```markdown
- `agent_tools.public.web`: `web_search`, `web_fetch`
```

with:

```markdown
- `agent_tools.public.web`: `web_search`, `web_extract`
```

Replace the web import example:

```python
from agent_tools.public.web import web_fetch, web_search
```

with:

```python
from agent_tools.public.web import web_extract, web_search
```

Add this short note under the public imports section:

```markdown
`web_search` returns metadata results only. Use `web_extract` to read page content. `web_fetch` is no longer part of the public agent tool surface.
```

- [ ] **Step 5: Run integration tests**

Run:

```bash
pytest tests/test_public_toolmessage_results.py::test_public_tool_entrypoints_register_runtime_for_injection \
  tests/test_public_toolmessage_results.py::test_web_fetch_is_not_public_tool \
  tests/test_agent_tools_public_imports.py::test_public_web_memory_and_skills_exports_existing_tool_objects \
  tests/test_agent_tools_public_imports.py::test_web_fetch_removed_from_public_surfaces \
  tests/test_web_tools_migration.py::test_base_tools_include_web_extract_not_web_fetch \
  tests/test_web_tools_migration.py::test_tool_limits_include_web_extract_not_web_fetch -v
```

Expected: PASS.

- [ ] **Step 6: Commit integration updates**

```bash
git add agent_tools/public/__init__.py agent_core/delegation.py agent_core/tool_limits.py agent_tools/README.md tests/test_agent_tools_public_imports.py tests/test_public_toolmessage_results.py tests/test_web_tools_migration.py
git commit -m "feat: register web_extract public surface"
```

---

### Task 6: Add Partial Success and Summarization Fallback Coverage

**Files:**
- Modify: `tests/test_web_tools_migration.py`
- Modify: `agent_tools/public/web.py`

- [ ] **Step 1: Add failing extraction edge-case tests**

Append to `tests/test_web_tools_migration.py`:

```python
def test_web_extract_returns_partial_success_for_mixed_results(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": urls[0],
                    "title": "Good",
                    "content": "good content",
                    "format": format,
                    "metadata": {},
                },
                {
                    "url": urls[1],
                    "final_url": urls[1],
                    "title": "",
                    "content": "",
                    "format": format,
                    "error": {"code": "backend_error", "message": "failed"},
                    "metadata": {},
                },
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())

    result = web.web_extract.func(
        ["https://example.com/ok", "https://example.com/fail"],
        runtime=_runtime("call-partial"),
    )

    assert result.artifact["ok"] is True
    assert result.artifact["data"]["total"] == 2
    assert result.artifact["data"]["results"][0]["error"] is None
    assert result.artifact["data"]["results"][1]["error"]["code"] == "backend_error"


def test_web_extract_skips_summarization_without_auxiliary_config(monkeypatch):
    import agent_tools.public.web as web

    long_content = "Long content. " * 600

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": urls[0],
                    "title": "Long",
                    "content": long_content,
                    "format": format,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())
    monkeypatch.delenv("AUXILIARY_WEB_EXTRACT_API_KEY", raising=False)
    monkeypatch.delenv("AUXILIARY_WEB_EXTRACT_BASE_URL", raising=False)
    monkeypatch.delenv("AUXILIARY_WEB_EXTRACT_MODEL", raising=False)

    result = web.web_extract.func(
        ["https://example.com/long"],
        min_length=100,
        max_chars_per_url=200,
        runtime=_runtime("call-no-aux"),
    )

    doc = result.artifact["data"]["results"][0]
    assert doc["processed"] is False
    assert doc["truncated"] is True
    assert doc["content"].startswith("Long content.")


def test_web_extract_summarization_failure_falls_back_to_bounded_raw(monkeypatch):
    import agent_tools.public.web as web

    class FakeBackend:
        name = "fake"

        def extract(self, urls, format):
            return [
                {
                    "url": urls[0],
                    "final_url": urls[0],
                    "title": "Long",
                    "content": "Raw page content. " * 400,
                    "format": format,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_API_KEY", "aux-key")
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_BASE_URL", "https://aux.example.com/v1")
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_MODEL", "aux-model")
    monkeypatch.setattr(web, "summarize_with_auxiliary", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom aux-key")))

    result = web.web_extract.func(
        ["https://example.com/long"],
        min_length=100,
        max_chars_per_url=120,
        runtime=_runtime("call-aux-fail"),
    )

    doc = result.artifact["data"]["results"][0]
    assert result.artifact["ok"] is True
    assert doc["processed"] is False
    assert doc["content"].startswith("Raw page content.")
    assert doc["truncated"] is True
    assert "aux-key" not in str(doc["metadata"])
    assert doc["metadata"]["warnings"]
```

- [ ] **Step 2: Run edge-case tests**

Run:

```bash
pytest tests/test_web_tools_migration.py::test_web_extract_returns_partial_success_for_mixed_results \
  tests/test_web_tools_migration.py::test_web_extract_skips_summarization_without_auxiliary_config \
  tests/test_web_tools_migration.py::test_web_extract_summarization_failure_falls_back_to_bounded_raw -v
```

Expected: PASS if Task 4 implementation already satisfies the behavior. If any test fails, change only `agent_tools/public/web.py` to match the tested behavior:

```python
# The relevant invariants:
# - A result list with at least one document without `error` returns tool_success.
# - Missing AUXILIARY_WEB_EXTRACT_* config skips summarization.
# - summarize_with_auxiliary exceptions append a redacted metadata warning and keep bounded raw content.
```

- [ ] **Step 3: Commit edge-case coverage**

```bash
git add agent_tools/public/web.py tests/test_web_tools_migration.py
git commit -m "test: cover web_extract partial success and fallback"
```

---

### Task 7: Run Focused and Full Verification

**Files:**
- No source files unless verification reveals a defect.

- [ ] **Step 1: Run all web migration tests**

Run:

```bash
pytest tests/test_web_tools_migration.py tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py -v
```

Expected: PASS.

- [ ] **Step 2: Run related integration tests**

Run:

```bash
pytest tests/test_cron_runner.py tests/test_tool_result.py tests/test_permissions_human_loop.py -v
```

Expected: PASS.

- [ ] **Step 3: Run repository test suite**

Run:

```bash
pytest
```

Expected: PASS. If unrelated pre-existing failures appear, record the failing test names and confirm they do not involve `agent_tools.public.web`, `agent_tools.web_toolkit`, `agent_tools.web`, `agent_core.delegation`, or `agent_core.tool_limits`.

- [ ] **Step 4: Check imports without provider SDK credentials**

Run:

```bash
python - <<'PY'
import agent_tools.public.web as web
import agent_core.delegation as delegation

names = {tool.name for tool in delegation.BASE_TOOLS}
assert hasattr(web, "web_search")
assert hasattr(web, "web_extract")
assert not hasattr(web, "web_fetch")
assert "web_search" in names
assert "web_extract" in names
assert "web_fetch" not in names
print("web imports ok")
PY
```

Expected output:

```text
web imports ok
```

- [ ] **Step 5: Review changed files**

Run:

```bash
git diff --stat
git diff -- agent_tools/public/web.py agent_tools/web_toolkit/backends.py agent_tools/web_toolkit/safety.py agent_tools/web_toolkit/content.py agent_core/delegation.py agent_core/tool_limits.py agent_tools/public/__init__.py agent_tools/README.md tests/test_web_tools_migration.py tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py
```

Expected: Diff only contains reference implementation web migration work and no unrelated worktree cleanup.

- [ ] **Step 6: Final commit if verification fixes were needed**

Only if Step 1-5 required additional edits:

```bash
git add agent_tools/public/web.py agent_tools/web_toolkit/backends.py agent_tools/web_toolkit/safety.py agent_tools/web_toolkit/content.py agent_core/delegation.py agent_core/tool_limits.py agent_tools/public/__init__.py agent_tools/README.md tests/test_web_tools_migration.py tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py
git commit -m "fix: complete reference implementation web migration verification"
```

---

## Self-Review Checklist

- [ ] Spec coverage: `web_search` and `web_extract` replace TinyFish `web_search` / `web_fetch`.
- [ ] Spec coverage: public tools use `@tool`, Pydantic schemas, injected `ToolRuntime`, and `tool_success` / `tool_failure`.
- [ ] Spec coverage: Firecrawl, Parallel, Tavily, and Exa adapters are lazily imported or HTTP-only.
- [ ] Spec coverage: `AGENT_WEB_BACKEND`, `WEB_BACKEND`, and auto-selection priority are tested.
- [ ] Spec coverage: missing credentials, missing SDKs, invalid backend, and backend errors return structured `tool_failure`.
- [ ] Spec coverage: unsafe URLs, embedded secrets, and unsafe final URLs are blocked.
- [ ] Spec coverage: base64 cleanup, per-URL output caps, truncation flags, optional summarization, and fallback warnings are tested.
- [ ] Spec coverage: `BASE_TOOLS` and `tool_limits` contain `web_extract`, not `web_fetch`.
- [ ] Placeholder scan: no unresolved placeholders or generic unexpanded testing instructions.
- [ ] Type consistency: tests call `web.web_extract.func(...)`; implementation exports `web_extract`; shim `agent_tools.web` remains the public compatibility shim.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-06-web-tools-migration.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using `superpowers:executing-plans`, with batch checkpoints.
