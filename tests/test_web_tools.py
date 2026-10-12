import asyncio
import json
from time import monotonic

import httpx
import pytest

from laplace.config import Settings
from laplace.llm.base import BudgetStop
from laplace.services.budget import TurnBudget
from laplace.services.web import SyncHttpxAdapter, TavilyClient, canonical_public_url
from laplace.tools.base import ToolFailure


def _adapter(handler):
    async def async_handler(request):
        return handler(request)

    return SyncHttpxAdapter(transport=httpx.MockTransport(async_handler))


def _budget() -> TurnBudget:
    return TurnBudget(settings=Settings(_env_file=None))


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
        client = TavilyClient("secret", 5, budget=_budget(), adapter=adapter)
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
            TavilyClient("", 5, budget=_budget(), adapter=adapter)
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
        client = TavilyClient("secret", 5, budget=_budget(), adapter=adapter)
        with pytest.raises(ToolFailure) as raised:
            client.search("query", max_results=1, topic="general")
    assert raised.value.error_code == code
    assert raised.value.retryable is retryable
    assert "secret" not in raised.value.message


def test_transport_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with _adapter(timeout) as adapter:
        client = TavilyClient("secret", 5, budget=_budget(), adapter=adapter)
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
        client = TavilyClient("secret", 5, budget=_budget(), adapter=adapter)
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


class _SlowDrip(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.cancelled = False
        self.closed = False
        self.chunks_sent = 0

    async def __aiter__(self):
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                self.chunks_sent += 1
                yield b" "
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("limiter", ["http", "turn"])
def test_slow_drip_has_total_deadline_and_actual_stream_cleanup(limiter) -> None:
    offset = [0.0]
    budget = TurnBudget(
        settings=Settings(_env_file=None, max_seconds=1),
        clock=lambda: monotonic() + offset[0],
    )
    timeout = 0.08 if limiter == "http" else 5
    if limiter == "turn":
        offset[0] = 0.92
    stream = _SlowDrip()

    with _adapter(lambda _request: httpx.Response(200, stream=stream)) as adapter:
        client = TavilyClient("secret", timeout, budget=budget, adapter=adapter)
        started = monotonic()
        if limiter == "turn":
            with pytest.raises(BudgetStop) as stopped:
                client.search("query", max_results=1, topic="general")
            assert stopped.value.reason == "time_limit"
        else:
            with pytest.raises(ToolFailure) as failed:
                client.search("query", max_results=1, topic="general")
            assert (failed.value.error_code, failed.value.retryable) == ("timeout", True)
        assert monotonic() - started < 0.8
    assert stream.cancelled and stream.closed
    assert stream.chunks_sent < 100


def test_expired_budget_prevents_network_request() -> None:
    elapsed = [0.0]
    budget = TurnBudget(
        settings=Settings(_env_file=None, max_seconds=1), clock=lambda: elapsed[0]
    )
    calls = []
    with _adapter(lambda request: calls.append(request)) as adapter:
        client = TavilyClient("secret", 5, budget=budget, adapter=adapter)
        elapsed[0] = 1.0
        with pytest.raises(BudgetStop) as stopped:
            client.search("query", max_results=1, topic="general")
    assert stopped.value.reason == "time_limit"
    assert calls == []


def test_response_arriving_at_budget_deadline_is_not_returned() -> None:
    elapsed = [0.0]
    budget = TurnBudget(
        settings=Settings(_env_file=None, max_seconds=1), clock=lambda: elapsed[0]
    )

    def response_at_deadline(_request):
        elapsed[0] = 1.0
        return httpx.Response(200, json={"results": []})

    with _adapter(response_at_deadline) as adapter:
        client = TavilyClient("secret", 5, budget=budget, adapter=adapter)
        with pytest.raises(BudgetStop) as stopped:
            client.search("query", max_results=1, topic="general")
    assert stopped.value.reason == "time_limit"
