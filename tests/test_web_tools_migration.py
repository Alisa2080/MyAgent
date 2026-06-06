import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _stub_httpx_for_web_imports(monkeypatch):
    if "httpx" not in sys.modules:
        monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace())


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


def test_tool_limits_include_web_extract_not_web_fetch(monkeypatch):
    import agent_core.tool_limits as tool_limits

    calls = []

    class FakeToolCallLimitMiddleware:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(tool_limits, "ToolCallLimitMiddleware", FakeToolCallLimitMiddleware)

    tool_limits.build_tool_call_limit_middleware()
    names = {call["tool_name"] for call in calls}

    assert "web_search" in names
    assert "web_extract" in names
    assert "web_fetch" not in names


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


def test_is_safe_url_blocks_unresolvable_hostname(monkeypatch):
    from agent_tools.web_toolkit import safety

    def raise_dns_error(*args, **kwargs):
        raise OSError("temporary DNS failure")

    monkeypatch.setattr(safety.socket, "getaddrinfo", raise_dns_error)

    ok, reason = safety.is_safe_url("https://unresolvable.example.test/docs")

    assert ok is False
    assert reason == "URL hostname could not be resolved"


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


def test_normalize_extract_result_reads_provider_content_fields():
    from agent_tools.web_toolkit.backends import normalize_extract_result

    tavily_doc = normalize_extract_result(
        "tavily",
        {"url": "https://example.com/a", "raw_content": "Tavily raw markdown"},
        requested_format="markdown",
    )
    parallel_doc = normalize_extract_result(
        "parallel",
        {"url": "https://example.com/b", "full_content": "Parallel full content"},
        requested_format="markdown",
    )
    parallel_excerpt_doc = normalize_extract_result(
        "parallel",
        {
            "url": "https://example.com/c",
            "excerpts": [{"text": "first excerpt"}, {"content": "second excerpt"}],
        },
        requested_format="markdown",
    )

    assert tavily_doc["content"] == "Tavily raw markdown"
    assert parallel_doc["content"] == "Parallel full content"
    assert parallel_excerpt_doc["content"] == "first excerpt\n\nsecond excerpt"


def test_firecrawl_search_flattens_current_response_shape():
    from agent_tools.web_toolkit.backends import FirecrawlBackend

    class FakeClient:
        def search(self, **kwargs):
            return {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Web result",
                            "url": "https://example.com",
                            "description": "Snippet",
                        }
                    ]
                },
            }

    backend = FirecrawlBackend(api_key="key", api_url=None)
    backend._client = lambda: FakeClient()

    response = backend.search("langchain", 5)

    assert response["results"][0]["title"] == "Web result"
    assert response["results"][0]["url"] == "https://example.com"


def test_firecrawl_extract_supports_current_scrape_method():
    from agent_tools.web_toolkit.backends import FirecrawlBackend

    calls = []

    class FakeClient:
        def scrape(self, **kwargs):
            calls.append(kwargs)
            return {"data": {"markdown": "Firecrawl markdown", "metadata": {"title": "Firecrawl"}}}

    backend = FirecrawlBackend(api_key="key", api_url=None)
    backend._client = lambda: FakeClient()

    docs = backend.extract(["https://example.com"], "markdown")

    assert calls == [{"url": "https://example.com", "formats": ["markdown"]}]
    assert docs[0]["content"] == "Firecrawl markdown"


def test_tavily_extract_uses_bearer_auth_and_markdown_format(monkeypatch):
    from agent_tools.web_toolkit.backends import TavilyBackend

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"url": "https://example.com", "raw_content": "Tavily markdown"}]}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("agent_tools.web_toolkit.backends.httpx.post", fake_post)

    docs = TavilyBackend(api_key="tvly-key").extract(["https://example.com"], "html")

    assert calls[0][0] == "https://api.tavily.com/extract"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer tvly-key"
    assert calls[0][1]["json"]["format"] == "markdown"
    assert "api_key" not in calls[0][1]["json"]
    assert docs[0]["content"] == "Tavily markdown"


def test_exa_extract_maps_html_request_to_text_content():
    from agent_tools.web_toolkit.backends import ExaBackend

    calls = []

    class FakeClient:
        def get_contents(self, urls, **kwargs):
            calls.append((urls, kwargs))
            return {"results": [{"url": urls[0], "text": "Exa text content"}]}

    backend = ExaBackend(api_key="exa-key")
    backend._client = lambda: FakeClient()

    docs = backend.extract(["https://example.com"], "html")

    assert calls == [(["https://example.com"], {"text": True, "highlights": True})]
    assert docs[0]["content"] == "Exa text content"


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
    monkeypatch.setattr(
        web,
        "is_safe_url",
        lambda url: (False, "private or local IP URLs are not allowed")
        if str(url).startswith("http://127.0.0.1")
        else (True, None),
    )

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
                    "content": "prefix data:image/png;base64," + ("A" * 200) + " " + ("B" * 200),
                    "format": format,
                    "truncated": False,
                    "processed": False,
                    "error": None,
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(web, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(web, "is_safe_url", lambda url: (True, None))

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
    monkeypatch.setattr(web, "is_safe_url", lambda url: (True, None))

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
    monkeypatch.setattr(web, "is_safe_url", lambda url: (True, None))
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
    monkeypatch.setattr(web, "is_safe_url", lambda url: (True, None))
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_API_KEY", "aux-key")
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_BASE_URL", "https://aux.example.com/v1")
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_MODEL", "aux-model")
    monkeypatch.setattr(
        web,
        "summarize_with_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom aux-key")),
    )

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
    assert doc["metadata"]["warnings"]


def test_web_toolkit_import_path_is_project_native():
    from agent_tools.web_toolkit import backends, content, safety

    assert hasattr(backends, "get_backend")
    assert hasattr(content, "clean_base64_images")
    assert hasattr(safety, "is_safe_url")
