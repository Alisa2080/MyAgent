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
    from agent_tools.web_hermes.safety import is_safe_url

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
    from agent_tools.web_hermes import safety

    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args, **kwargs: [])

    ok, reason = safety.is_safe_url("https://example.com/docs")

    assert ok is True
    assert reason is None


def test_contains_embedded_secret_detects_raw_and_encoded_values():
    from agent_tools.web_hermes.safety import contains_embedded_secret

    assert contains_embedded_secret("https://example.com/?api_key=abc")
    assert contains_embedded_secret("https://example.com/?q=sk-abc123")
    assert contains_embedded_secret("https://example.com/?q=%73%6b-abc123")
    assert not contains_embedded_secret("https://example.com/?q=public")


def test_clean_base64_images_replaces_data_uri_payloads():
    from agent_tools.web_hermes.content import clean_base64_images

    text = "before data:image/png;base64," + ("A" * 200) + " after"

    cleaned = clean_base64_images(text)

    assert "[BASE64_IMAGE_REMOVED]" in cleaned
    assert "A" * 80 not in cleaned


def test_explicit_backend_selection_honors_agent_web_backend(monkeypatch):
    from agent_tools.web_hermes import backends

    monkeypatch.setenv("AGENT_WEB_BACKEND", "parallel")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-key")
    monkeypatch.delenv("WEB_BACKEND", raising=False)

    selected = backends.select_backend_name()

    assert selected == "parallel"


def test_backend_auto_selection_priority(monkeypatch):
    from agent_tools.web_hermes import backends

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
    from agent_tools.web_hermes import backends

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
    from agent_tools.web_hermes import backends

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
    from agent_tools.web_hermes.backends import normalize_search_results

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
                    "content": "prefix data:image/png;base64," + ("A" * 200) + " " + ("B" * 200),
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
