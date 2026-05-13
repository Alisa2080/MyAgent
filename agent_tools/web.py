import os
from functools import lru_cache
from typing import Any

import dotenv
from langchain.tools import tool
from pydantic import BaseModel, Field
from tinyfish import TinyFish

from agent_tools.common import truncate
from agent_tools.tool_output import tool_error, tool_ok

dotenv.load_dotenv()


class SearchInput(BaseModel):
    query: str = Field(description="The search query.")
    limit: int = Field(default=5, description="Maximum number of search results to return.")


class FetchInput(BaseModel):
    urls: list[str] = Field(description="A list of URLs to fetch.")
    max_chars_per_url: int = Field(default=4000, description="Maximum characters to keep for each fetched page.")


@lru_cache(maxsize=1)
def _get_client() -> TinyFish:
    api_key = os.getenv("TINYFISH_API_KEY")
    if not api_key:
        raise RuntimeError("TINYFISH_API_KEY environment variable is not set or empty")
    return TinyFish()


def _safe_get(obj: Any, field: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


@tool("web_search", args_schema=SearchInput)
def web_search(query: str, limit: int = 5) -> str:
    """Search the web with TinyFish. Returns JSON: status, message, data."""
    try:
        client = _get_client()
        response = client.search.query(query=query)
    except Exception as exc:
        return tool_error("web_search", f"web_search failed: {exc}")
    raw_results = (_safe_get(response, "results", []) or [])[: max(limit, 0)]
    results = []
    for item in raw_results:
        results.append(
            {
                "title": _safe_get(item, "title", "(untitled)"),
                "url": _safe_get(item, "url", ""),
                "snippet": _safe_get(item, "snippet", "") or _safe_get(item, "text", ""),
            }
        )
    return tool_ok(
        "web_search",
        data={"query": query, "results": results, "total": len(results)},
        message="Search completed.",
    )


@tool("web_fetch", args_schema=FetchInput)
def web_fetch(urls: list[str], max_chars_per_url: int = 4000) -> str:
    """Fetch page contents with TinyFish. Returns JSON: status, message, data."""
    try:
        client = _get_client()
        response = client.fetch.get_contents(urls=urls)
    except Exception as exc:
        return tool_error("web_fetch", f"web_fetch failed: {exc}")
    raw_results = _safe_get(response, "results", []) or []
    results = []
    for item in raw_results:
        text = _safe_get(item, "text", "")
        results.append(
            {
                "title": _safe_get(item, "title", "(untitled)"),
                "url": _safe_get(item, "url", ""),
                "content": truncate(text, max(max_chars_per_url, 0)),
                "truncated": len(text) > max(max_chars_per_url, 0),
            }
        )
    return tool_ok(
        "web_fetch",
        data={"urls": urls, "results": results, "total": len(results)},
        message="Fetch completed.",
    )
