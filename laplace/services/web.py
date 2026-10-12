"""Bounded synchronous client for Tavily Search and Extract."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from laplace.tools.base import ToolFailure

if TYPE_CHECKING:
    from laplace.services.budget import TurnBudget

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 500
MAX_SEARCH_CONTENT_CHARS = 8_000
MAX_EXTRACT_CONTENT_CHARS = 100_000
_MAX_TIMEOUT_SECONDS = 30.0
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    content: str
    score: float | None


@dataclass(frozen=True, slots=True)
class ExtractResult:
    url: str
    content: str


def canonical_public_url(value: str) -> str:
    """Validate and canonicalize a public HTTP(S) URL without resolving DNS."""

    if not isinstance(value, str):
        raise ValueError("url must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > 2_048:
        raise ValueError("url must contain at most 2048 characters")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ValueError("url must not contain whitespace or control characters")
    if "\\" in candidate:
        raise ValueError("url must not contain backslashes")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("url must include a host")

    raw_host = parsed.hostname.rstrip(".").lower()
    if not raw_host:
        raise ValueError("url must include a host")
    if raw_host == "localhost" or raw_host.endswith(".localhost"):
        raise ValueError("localhost URLs are not allowed")
    if "%" in raw_host:
        raise ValueError("scoped IP addresses are not allowed")

    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("url host is malformed") from exc
        labels = host.split(".")
        if len(host) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ValueError("url host is malformed")
        rendered_host = host
    else:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("url must use a public IP address")
        rendered_host = f"[{address.compressed}]" if address.version == 6 else address.compressed

    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("url port is invalid")
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = rendered_host if port is None or default_port else f"{rendered_host}:{port}"
    normalized = SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    return urlunsplit(normalized)


class SyncHttpxAdapter:
    """Synchronous bridge to cancellable async HTTP with bounded total reads."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes < 1 or max_response_bytes > MAX_RESPONSE_BYTES:
            raise ValueError(f"max_response_bytes must be between 1 and {MAX_RESPONSE_BYTES}")
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    def close(self) -> None:
        if self._transport is not None:
            asyncio.run(self._transport.aclose())

    def __enter__(self) -> SyncHttpxAdapter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ToolFailure(
                "configuration_error", "Web request timeout is not configured safely."
            )

        return asyncio.run(
            self._post_json_async(
                url, headers=headers, payload=payload, timeout_seconds=timeout_seconds
            )
        )

    async def _post_json_async(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        json=payload,
                        timeout=timeout_seconds,
                    ) as response:
                        self._raise_for_status(response.status_code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self._max_response_bytes:
                                raise ToolFailure(
                                    "upstream_error",
                                    "Web service returned a response that was too large.",
                                )
                            body.extend(chunk)
        except ToolFailure:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ToolFailure(
                "timeout",
                "Web service request timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ToolFailure(
                "upstream_error",
                "Web service could not be reached.",
                retryable=True,
            ) from exc

        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolFailure(
                "upstream_error",
                "Web service returned a malformed response.",
            ) from exc
        if not isinstance(decoded, dict):
            raise ToolFailure("upstream_error", "Web service returned a malformed response.")
        return decoded

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code == 400:
            raise ToolFailure("invalid_input", "Web service rejected the request.")
        if status_code in {401, 403}:
            raise ToolFailure(
                "configuration_error",
                "Web service credentials were rejected.",
            )
        if status_code in {408, 504}:
            raise ToolFailure(
                "timeout",
                "Web service request timed out.",
                retryable=True,
            )
        if status_code == 429:
            raise ToolFailure(
                "upstream_error",
                "Web service rate limit was reached.",
                retryable=True,
            )
        if 500 <= status_code < 600:
            raise ToolFailure(
                "upstream_error",
                "Web service is temporarily unavailable.",
                retryable=True,
            )
        raise ToolFailure("upstream_error", "Web service rejected the request.")


class TavilyClient:
    """Small Tavily REST client; the API key is sent only in Authorization."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        *,
        budget: TurnBudget,
        adapter: SyncHttpxAdapter | None = None,
    ) -> None:
        if not api_key.strip():
            raise ToolFailure("configuration_error", "Web tools are not configured.")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ToolFailure(
                "configuration_error", "Web request timeout is not configured safely."
            )
        self._budget = budget
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._adapter = adapter or SyncHttpxAdapter()
        self._owns_adapter = adapter is None

    def close(self) -> None:
        if self._owns_adapter:
            self._adapter.close()

    def __enter__(self) -> TavilyClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def search(
        self,
        query: str,
        *,
        max_results: int,
        topic: Literal["general", "news"],
    ) -> tuple[SearchResult, ...]:
        payload = self._post(
            TAVILY_SEARCH_URL,
            {"query": query, "max_results": max_results, "topic": topic},
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ToolFailure("upstream_error", "Web service returned malformed search results.")

        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for raw in raw_results:
            result = self._parse_search_result(raw)
            if result is None or result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            results.append(result)
            if len(results) == max_results:
                break
        return tuple(results)

    def extract(
        self,
        urls: tuple[str, ...],
        *,
        query: str,
        chunks_per_source: int,
    ) -> tuple[ExtractResult, ...]:
        canonical_urls = tuple(canonical_public_url(url) for url in urls)
        payload = self._post(
            TAVILY_EXTRACT_URL,
            {
                "urls": list(canonical_urls),
                "query": query,
                "chunks_per_source": chunks_per_source,
            },
        )
        raw_results = payload.get("results")
        failed_results = payload.get("failed_results", [])
        if not isinstance(raw_results, list) or not isinstance(failed_results, list):
            raise ToolFailure("upstream_error", "Web service returned malformed extract results.")

        results: list[ExtractResult] = []
        seen_urls: set[str] = set()
        for raw in raw_results:
            result = self._parse_extract_result(raw)
            if result is None or result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            results.append(result)

        if not results and (failed_results or canonical_urls):
            raise ToolFailure(
                "upstream_error",
                "Web service could not extract the requested page.",
            )
        return tuple(results)

    def _post(self, url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._budget.check()
        try:
            return self._adapter.post_json(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=min(self._timeout_seconds, self._budget.remaining_seconds()),
            )
        finally:
            self._budget.check()

    @staticmethod
    def _parse_search_result(raw: object) -> SearchResult | None:
        if not isinstance(raw, dict):
            raise ToolFailure("upstream_error", "Web service returned malformed search results.")
        title = raw.get("title")
        url = raw.get("url")
        content = raw.get("content")
        score = raw.get("score")
        if not isinstance(title, str) or not isinstance(url, str) or not isinstance(content, str):
            raise ToolFailure("upstream_error", "Web service returned malformed search results.")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
            raise ToolFailure("upstream_error", "Web service returned malformed search results.")
        try:
            canonical_url = canonical_public_url(url)
        except ValueError:
            return None
        normalized_title = " ".join(title.split())[:MAX_TITLE_CHARS]
        normalized_content = " ".join(content.split())[:MAX_SEARCH_CONTENT_CHARS]
        normalized_score = None if score is None else float(score)
        if normalized_score is not None and not math.isfinite(normalized_score):
            raise ToolFailure("upstream_error", "Web service returned malformed search results.")
        return SearchResult(
            title=normalized_title,
            url=canonical_url,
            content=normalized_content,
            score=normalized_score,
        )

    @staticmethod
    def _parse_extract_result(raw: object) -> ExtractResult | None:
        if not isinstance(raw, dict):
            raise ToolFailure("upstream_error", "Web service returned malformed extract results.")
        url = raw.get("url")
        content = raw.get("raw_content")
        if not isinstance(url, str) or not isinstance(content, str):
            raise ToolFailure("upstream_error", "Web service returned malformed extract results.")
        try:
            canonical_url = canonical_public_url(url)
        except ValueError:
            return None
        normalized_content = content.strip()[:MAX_EXTRACT_CONTENT_CHARS]
        if not normalized_content:
            return None
        return ExtractResult(url=canonical_url, content=normalized_content)
