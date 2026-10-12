"""Explicit offline provider; synthetic usage is accounted at known-zero cost."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

from laplace.llm.base import BudgetStop, CallOptions, LLMResult


def account_mock_result(
    result: LLMResult,
    messages: list[dict[str, str]],
    *,
    options: CallOptions,
    json_schema: dict[str, Any] | None = None,
    provider: str = "mock",
) -> LLMResult:
    """Account one synthetic response, also used by context-dependent fakes.

    Token/latency metrics remain useful reports, but scripted cost is never paid
    pricing evidence. A named synthetic model has one explicit zero-rate alias;
    an unknown-usage script still exercises the real recorder's fail-closed path.
    """
    options.check()
    model = result.model or provider
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": options.max_completion_tokens,
    }
    if json_schema is not None:
        payload["json_schema"] = json_schema
    attempt_id = options.observer.begin(
        call_group_id=str(uuid4()),
        attempt_index=0,
        provider=provider,
        requested_model=model,
        payload=payload,
        pricing={model: (0.0, 0.0)},
        pricing_version="synthetic-known-zero-v1",
    )
    usage_known = (
        result.usage_known
        and result.extra.get("usage_known", True) is not False
        and type(result.prompt_tokens) is int
        and result.prompt_tokens >= 0
        and type(result.completion_tokens) is int
        and result.completion_tokens >= 0
    )
    try:
        options.observer.finish(
            attempt_id,
            returned_model=model,
            prompt_tokens=result.prompt_tokens if usage_known else None,
            completion_tokens=result.completion_tokens if usage_known else None,
            latency_ms=result.latency_ms,
            outcome="success",
        )
    except BudgetStop:
        raise
    except Exception:
        raise BudgetStop("budget_unavailable") from None
    options.check()
    if not usage_known:
        raise BudgetStop("budget_unavailable")
    return replace(
        result, model=model, cost_usd=0.0, attempt_ids=(attempt_id,), usage_known=True
    )


class MockLLM:
    """Deterministic scripted or heuristic provider, selected only as mock."""

    name = "mock"

    def __init__(self, script: list[dict[str, Any] | str | LLMResult] | None = None):
        self._script = list(script) if script else None
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: CallOptions,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        options.check()
        self.calls.append({"messages": messages, "json_schema": json_schema, "options": options})
        if self._script is not None:
            if not self._script:
                raise RuntimeError("MockLLM script exhausted")
            item = self._script.pop(0)
            if isinstance(item, LLMResult):
                result = item
            elif isinstance(item, dict):
                result = LLMResult(
                    parsed=item, model="mock", prompt_tokens=10, completion_tokens=10
                )
            else:
                result = LLMResult(
                    content=item, model="mock", prompt_tokens=10, completion_tokens=10
                )
        elif json_schema is not None:
            props = json_schema.get("properties", {})
            result = LLMResult(
                parsed={"action": "final", "final_answer": "[mock] xong"}
                if "action" in props else {},
                model="mock",
            )
        else:
            last = messages[-1]["content"] if messages else ""
            result = LLMResult(content=f"[mock] {last[:200]}", model="mock")
        return account_mock_result(result, messages, options=options, json_schema=json_schema)
