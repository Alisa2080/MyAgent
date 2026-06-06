from __future__ import annotations

import os
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


def bound_content(content: str, max_chars: int = DEFAULT_MAX_CHARS_PER_URL) -> tuple[str, bool]:
    value = "" if content is None else str(content)
    limit = max(int(max_chars or 0), 0)
    if limit <= 0:
        return "", bool(value)
    if len(value) <= limit:
        return value, False

    marker = "\n\n[Content truncated for context management.]"
    if limit <= len(marker):
        return value[:limit], True
    return value[: limit - len(marker)].rstrip() + marker, True


def should_summarize(
    content: str,
    *,
    use_llm_processing: bool,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    model: str | None = None,
) -> bool:
    if not use_llm_processing:
        return False
    if len(content or "") < max(int(min_length or 0), 0):
        return False
    return bool(model or os.getenv("AUXILIARY_WEB_EXTRACT_MODEL"))


def summarize_with_auxiliary(
    content: str,
    *,
    url: str = "",
    title: str = "",
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 60.0,
) -> str:
    selected_model = model or os.getenv("AUXILIARY_WEB_EXTRACT_MODEL")
    selected_base_url = base_url or os.getenv("AUXILIARY_WEB_EXTRACT_BASE_URL")
    selected_api_key = api_key or os.getenv("AUXILIARY_WEB_EXTRACT_API_KEY")
    if not selected_model or not selected_base_url or not selected_api_key:
        raise RuntimeError("Auxiliary web extract model, base URL, and API key are required")

    endpoint = selected_base_url.rstrip("/") + "/chat/completions"
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
        headers={"Authorization": f"Bearer {selected_api_key}", "Content-Type": "application/json"},
        json={
            "model": selected_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return str(payload["choices"][0]["message"]["content"]).strip()


def process_extracted_content(
    content: str,
    *,
    url: str = "",
    title: str = "",
    max_chars: int = DEFAULT_MAX_CHARS_PER_URL,
    use_llm_processing: bool = False,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    cleaned = clean_base64_images(content)
    metadata: dict[str, Any] = {"processed": False, "truncated": False}
    if should_summarize(cleaned, use_llm_processing=use_llm_processing, min_length=min_length, model=model):
        try:
            summarized = summarize_with_auxiliary(cleaned, url=url, title=title, model=model)
        except Exception as exc:
            metadata["processing_warning"] = f"Auxiliary summarization failed: {exc}"
        else:
            cleaned = summarized
            metadata["processed"] = True

    bounded, truncated = bound_content(cleaned, max_chars)
    metadata["truncated"] = truncated
    return bounded, metadata
