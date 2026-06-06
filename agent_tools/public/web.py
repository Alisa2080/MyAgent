import os
from typing import Literal

import dotenv
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_tools.file_toolkit.redact import redact_sensitive_text
from agent_tools.shared.tool_result import tool_failure, tool_success
from agent_tools.web_hermes.backends import BackendConfigurationError, get_backend
from agent_tools.web_hermes.content import (
    DEFAULT_MAX_CHARS_PER_URL,
    DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    bound_content,
    clean_base64_images,
    should_summarize,
    summarize_with_auxiliary,
)
from agent_tools.web_hermes.safety import is_safe_url


dotenv.load_dotenv()


class SearchInput(BaseModel):
    query: str = Field(description="The search query.")
    limit: int = Field(default=5, description="Maximum number of search results to return.")


class ExtractInput(BaseModel):
    urls: list[str] = Field(description="A list of URLs to extract.")
    format: Literal["markdown", "html"] = Field(default="markdown", description="Output format to request.")
    use_llm_processing: bool = Field(default=True, description="Whether to use optional LLM post-processing.")
    model: str | None = Field(default=None, description="Optional auxiliary model name.")
    min_length: int = Field(
        default=DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
        description="Minimum content length for summarization.",
    )
    max_chars_per_url: int = Field(
        default=DEFAULT_MAX_CHARS_PER_URL,
        description="Maximum characters to keep per URL.",
    )


def _redact(message: object) -> str:
    text = redact_sensitive_text(str(message))
    for env_name in (
        "AUXILIARY_WEB_EXTRACT_API_KEY",
        "FIRECRAWL_API_KEY",
        "PARALLEL_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
    ):
        secret = os.getenv(env_name)
        if secret:
            text = text.replace(secret, "***")
    return text


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

    original_content = result["content"]
    cleaned = clean_base64_images(original_content)
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
            result["metadata"].setdefault("warnings", []).append(
                f"auxiliary summarization failed: {_redact(exc)}"
            )

    bounded, truncated = bound_content(cleaned, max_chars_per_url)
    result["content"] = bounded
    result["truncated"] = bool(truncated or result["truncated"] or cleaned != original_content)
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
        return tool_failure(
            "web_extract",
            f"web_extract failed: {_redact(exc)}",
            code="backend_error",
            runtime=runtime,
        )

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
