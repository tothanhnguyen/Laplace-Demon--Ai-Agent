"""LLM-facing web research tools backed by Tavily REST APIs."""

from typing import Literal

from pydantic import Field

from laplace.config import Settings
from laplace.services.web import TavilyClient, canonical_public_url
from laplace.tools.base import ToolContext, ToolFailure, ToolModel, ToolPayload, tool


class WebSearchArgs(ToolModel):
    query: str = Field(min_length=2, max_length=500)
    max_results: int = Field(default=5, ge=1, le=5)
    topic: Literal["general", "news"] = "general"


class WebSearchItem(ToolModel):
    title: str
    url: str
    content: str
    score: float | None = None


class WebSearchOutput(ToolModel):
    query: str
    results: list[WebSearchItem]


class FetchPageArgs(ToolModel):
    url: str = Field(min_length=1, max_length=2_048)
    query: str = Field(min_length=2, max_length=500)


class FetchPageItem(ToolModel):
    url: str
    content: str


class FetchPageOutput(ToolModel):
    query: str
    results: list[FetchPageItem]


@tool(
    name="web_search",
    purpose="Search the public web and return bounded evidence with source URLs.",
    when_to_use=(
        "Use for current or externally verifiable facts not present in conversation context."
    ),
    when_not_to_use=(
        "Do not use for private resources, logged-in pages, or when no web evidence is needed."
    ),
    risk_level="low",
    args_model=WebSearchArgs,
    result_model=WebSearchOutput,
)
def web_search(args: WebSearchArgs, _context: ToolContext) -> ToolPayload:
    settings = Settings()
    query = args.query.strip()
    if len(query) < 2:
        raise ToolFailure("invalid_input", "query must contain at least two characters.")
    maximum = min(args.max_results, settings.web_max_results)
    with TavilyClient(settings.tavily_api_key, settings.http_timeout_seconds) as client:
        results = client.search(query, max_results=maximum, topic=args.topic)
    data = {
        "query": query,
        "results": [
            {
                "title": item.title,
                "url": item.url,
                "content": item.content,
                "score": item.score,
            }
            for item in results
        ],
    }
    return ToolPayload(data=data, sources=tuple(item.url for item in results))


@tool(
    name="fetch_page",
    purpose="Extract bounded relevant text from one public web page with its source URL.",
    when_to_use="Use after search when a specific public result needs stronger evidence.",
    when_not_to_use="Do not use for private, local, authenticated, or arbitrary non-web resources.",
    risk_level="low",
    args_model=FetchPageArgs,
    result_model=FetchPageOutput,
)
def fetch_page(args: FetchPageArgs, _context: ToolContext) -> ToolPayload:
    settings = Settings()
    query = args.query.strip()
    if len(query) < 2:
        raise ToolFailure("invalid_input", "query must contain at least two characters.")
    try:
        url = canonical_public_url(args.url)
    except ValueError as exc:
        raise ToolFailure("invalid_input", str(exc)) from exc
    with TavilyClient(settings.tavily_api_key, settings.http_timeout_seconds) as client:
        results = client.extract(
            (url,),
            query=query,
            chunks_per_source=settings.web_max_chunks,
        )
    data = {
        "query": query,
        "results": [{"url": item.url, "content": item.content} for item in results],
    }
    return ToolPayload(data=data, sources=tuple(item.url for item in results))
