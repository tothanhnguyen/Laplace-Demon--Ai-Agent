import json

import httpx
import pytest

from laplace.services.web import SyncHttpxAdapter, TavilyClient, canonical_public_url
from laplace.tools.base import ToolFailure


def _adapter(handler):
    return SyncHttpxAdapter(transport=httpx.MockTransport(handler))


def test_search_preserves_provider_order_and_authorization() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Second",
                        "url": "https://b.example/x#frag",
                        "content": " B ",
                        "score": 0.8,
                    },
                    {"title": "First", "url": "https://a.example/", "content": " A ", "score": 0.9},
                ]
            },
        )

    with _adapter(handler) as adapter:
        client = TavilyClient("secret", 5, adapter=adapter)
        results = client.search("current fact", max_results=2, topic="general")

    assert [item.url for item in results] == ["https://b.example/x", "https://a.example/"]
    assert seen == {
        "authorization": "Bearer secret",
        "body": {"query": "current fact", "max_results": 2, "topic": "general"},
    }


def test_missing_key_makes_no_http_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    with _adapter(handler) as adapter:
        with pytest.raises(ToolFailure) as raised:
            TavilyClient("", 5, adapter=adapter)
    assert raised.value.error_code == "configuration_error"
    assert calls == 0


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "invalid_input", False),
        (401, "configuration_error", False),
        (429, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
def test_http_status_mapping(status: int, code: str, retryable: bool) -> None:
    with _adapter(lambda _request: httpx.Response(status, text="no secret")) as adapter:
        client = TavilyClient("secret", 5, adapter=adapter)
        with pytest.raises(ToolFailure) as raised:
            client.search("query", max_results=1, topic="general")
    assert raised.value.error_code == code
    assert raised.value.retryable is retryable
    assert "secret" not in raised.value.message


def test_transport_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with _adapter(timeout) as adapter:
        client = TavilyClient("secret", 5, adapter=adapter)
        with pytest.raises(ToolFailure) as raised:
            client.search("query", max_results=1, topic="general")
    assert (raised.value.error_code, raised.value.retryable) == ("timeout", True)


def test_extract_full_failure_and_malformed_response() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"results": [], "failed_results": [{"url": "x"}]}),
            httpx.Response(200, content=b"not-json"),
        ]
    )
    with _adapter(lambda _request: next(responses)) as adapter:
        client = TavilyClient("secret", 5, adapter=adapter)
        with pytest.raises(ToolFailure) as failed:
            client.extract(("https://example.com",), query="query", chunks_per_source=2)
        with pytest.raises(ToolFailure) as malformed:
            client.search("query", max_results=1, topic="general")
    assert failed.value.error_code == "upstream_error"
    assert malformed.value.error_code == "upstream_error"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://169.254.1.2/a",
        "https://user:password@example.com/a",
    ],
)
def test_url_validator_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(ValueError):
        canonical_public_url(url)
