from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from agent_tools.file_toolkit.redact import redact_sensitive_text


VALID_BACKENDS = {"parallel", "firecrawl", "tavily", "exa"}
AUTO_BACKEND_ENV_ORDER = (
    ("firecrawl", ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL")),
    ("parallel", ("PARALLEL_API_KEY",)),
    ("tavily", ("TAVILY_API_KEY",)),
    ("exa", ("EXA_API_KEY",)),
)


class BackendConfigurationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "missing_configuration"):
        super().__init__(redact_sensitive_text(message))
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


def _redacted_backend_error(backend: str, operation: str, exc: Exception) -> BackendConfigurationError:
    return BackendConfigurationError(
        f"{backend} {operation} failed: {redact_sensitive_text(str(exc))}",
        code="backend_error",
    )


def select_backend_name() -> str:
    explicit = (os.getenv("AGENT_WEB_BACKEND") or os.getenv("WEB_BACKEND") or "").strip().lower()
    if explicit:
        if explicit not in VALID_BACKENDS:
            valid = ", ".join(sorted(VALID_BACKENDS))
            raise BackendConfigurationError(
                f"Unsupported web backend '{explicit}'. Valid values: {valid}.",
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
        for key in ("score", "published_date", "publishedDate", "source", "id"):
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
    url = str(_obj_get(item, "url", "source_url", "sourceURL", default="") or "")
    final_url = str(_obj_get(item, "final_url", "finalUrl", "sourceURL", "source_url", "url", default=url) or url)
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
        "truncated": bool(_obj_get(item, "truncated", default=False)),
        "processed": bool(_obj_get(item, "processed", default=False)),
        "error": _obj_get(item, "error", default=None),
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
            raise BackendConfigurationError(
                "Firecrawl backend selected but firecrawl SDK is not installed.",
                code="missing_dependency",
            ) from exc
        client_class = getattr(module, "FirecrawlApp", None) or getattr(module, "Firecrawl", None)
        if client_class is None:
            raise BackendConfigurationError(
                "Installed firecrawl package does not expose FirecrawlApp.",
                code="missing_dependency",
            )
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_url:
            kwargs["api_url"] = self.api_url
        return client_class(**kwargs)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            response = self._client().search(query=query, limit=limit)
        except BackendConfigurationError:
            raise
        except Exception as exc:
            raise _redacted_backend_error(self.name, "search", exc) from exc
        raw = _obj_get(response, "data", "results", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        client = self._client()
        docs = []
        for url in urls:
            try:
                response = client.scrape_url(url, formats=[format])
            except Exception as exc:
                raise _redacted_backend_error(self.name, "extract", exc) from exc
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
            raise BackendConfigurationError(
                "Parallel backend selected but parallel SDK is not installed.",
                code="missing_dependency",
            ) from exc
        client_class = getattr(module, "Parallel", None) or getattr(module, "Client", None)
        if client_class is None:
            raise BackendConfigurationError(
                "Installed parallel package does not expose a client class.",
                code="missing_dependency",
            )
        return client_class(api_key=self.api_key)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        client = self._client()
        method = getattr(client, "search", None)
        if method is None:
            raise BackendConfigurationError("Parallel client does not expose search.", code="missing_dependency")
        try:
            response = method(query=query, limit=limit)
        except Exception as exc:
            raise _redacted_backend_error(self.name, "search", exc) from exc
        raw = _obj_get(response, "results", "data", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        client = self._client()
        method = getattr(client, "extract", None) or getattr(client, "scrape", None)
        if method is None:
            raise BackendConfigurationError("Parallel client does not expose extract.", code="missing_dependency")
        try:
            response = method(urls=urls, format=format)
        except Exception as exc:
            raise _redacted_backend_error(self.name, "extract", exc) from exc
        raw = _obj_get(response, "results", "data", default=response)
        return [normalize_extract_result(self.name, item, requested_format=format) for item in _as_list(raw)]


@dataclass
class TavilyBackend:
    api_key: str
    name: str = "tavily"

    def search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            response = httpx.post(
                "https://api.tavily.com/search",
                json={"api_key": self.api_key, "query": query, "max_results": limit},
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as exc:
            raise _redacted_backend_error(self.name, "search", exc) from exc
        payload = response.json()
        raw = payload.get("results", [])
        return {"backend": self.name, "results": normalize_search_results(self.name, raw, limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        try:
            response = httpx.post(
                "https://api.tavily.com/extract",
                json={"api_key": self.api_key, "urls": urls, "extract_depth": "advanced", "format": format},
                timeout=60.0,
            )
            response.raise_for_status()
        except Exception as exc:
            raise _redacted_backend_error(self.name, "extract", exc) from exc
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
                raise BackendConfigurationError(
                    "Exa backend selected but Exa SDK is not installed.",
                    code="missing_dependency",
                ) from exc
        client_class = getattr(module, "Exa", None)
        if client_class is None:
            raise BackendConfigurationError("Installed Exa package does not expose Exa.", code="missing_dependency")
        return client_class(api_key=self.api_key)

    def search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            response = self._client().search(query, num_results=limit)
        except BackendConfigurationError:
            raise
        except Exception as exc:
            raise _redacted_backend_error(self.name, "search", exc) from exc
        raw = _obj_get(response, "results", default=response)
        return {"backend": self.name, "results": normalize_search_results(self.name, _as_list(raw), limit=limit)}

    def extract(self, urls: list[str], format: str) -> list[dict[str, Any]]:
        try:
            response = self._client().get_contents(urls, text=format == "markdown", highlights=True)
        except BackendConfigurationError:
            raise
        except Exception as exc:
            raise _redacted_backend_error(self.name, "extract", exc) from exc
        raw = _obj_get(response, "results", default=response)
        return [normalize_extract_result(self.name, item, requested_format=format) for item in _as_list(raw)]


def get_backend() -> WebBackend:
    name = select_backend_name()
    if name == "firecrawl":
        api_key = os.getenv("FIRECRAWL_API_KEY")
        api_url = os.getenv("FIRECRAWL_API_URL")
        if not api_key and not api_url:
            raise BackendConfigurationError(
                "Firecrawl backend requires FIRECRAWL_API_KEY or FIRECRAWL_API_URL.",
                code="missing_configuration",
            )
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

