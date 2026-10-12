"""OpenAI-compatible adapter with durable physical-attempt accounting."""

import asyncio
import json
import math
import re
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import uuid4

from laplace.llm.base import BudgetStop, CallOptions, LLMResult
from laplace.llm.presets import PRESETS

RATE_LIMIT_MAX_RETRIES = 5
_OUTPUT_CAP_PARAMETERS = {"max_tokens", "max_completion_tokens"}


def _retry_delay_from(error: Exception, attempt: int) -> float:
    """Retain the existing bounded rate-limit backoff without logging bodies."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(error))
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return min(15.0 * (attempt + 1), 90.0)


def _valid_tokens(value: Any) -> bool:
    return type(value) is int and value >= 0


def _compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Mapping[str, tuple[float, float]],
) -> float:
    """Report known USD/1M pricing; an unknown alias is never free."""
    price = pricing.get(model)
    if price is None:
        raise BudgetStop("budget_unavailable")
    input_rate, output_rate = (Decimal(str(rate)) for rate in price)
    return float(
        (prompt_tokens * input_rate + completion_tokens * output_rate) / Decimal(1_000_000)
    )


def _format_rejected(error: Exception) -> bool:
    """Fallback only for a rejection naming response_format, never output caps."""
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return False
    detail = body.get("error", body)
    if not isinstance(detail, dict):
        return False
    parameter = detail.get("param")
    if isinstance(parameter, str) and parameter in _OUTPUT_CAP_PARAMETERS:
        return False
    if parameter == "response_format":
        return True
    message = detail.get("message")
    if not isinstance(message, str):
        return False
    message = message.lower()
    if any(parameter in message for parameter in _OUTPUT_CAP_PARAMETERS):
        return False
    return "response_format" in message and any(
        word in message
        for word in (
            "unsupported", "not supported", "unknown", "unrecognized", "invalid",
            "not allowed", "unexpected", "not permitted",
        )
    )


class OpenAIProvider:
    """Synchronous boundary backed by cancellable async SDK I/O.

    A client lives on one asyncio.run loop and is closed before that loop exits.
    SDK retries are disabled: only the existing five 429 retries and one relevant
    response-format fallback are available, each with its own reservation.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        name: str = "openai",
        pricing: Mapping[str, tuple[float, float]] | None = None,
        output_cap_parameter: str | None = None,
        pricing_version: str | None = None,
    ):
        if not api_key:
            raise RuntimeError("Missing API key for configured LLM provider.")
        preset = PRESETS.get(name)
        if pricing is None:
            pricing = preset.pricing if preset else {}
        if output_cap_parameter is None:
            output_cap_parameter = preset.output_cap_parameter if preset else None
        if pricing_version is None:
            pricing_version = preset.pricing_version if preset else None
        if (
            model not in pricing
            or output_cap_parameter not in _OUTPUT_CAP_PARAMETERS
            or not pricing_version
        ):
            raise ValueError("Unsupported LLM model or pricing profile configuration.")
        for rates in pricing.values():
            if len(rates) != 2 or any(
                isinstance(rate, bool) or not math.isfinite(rate) or rate < 0 for rate in rates
            ):
                raise ValueError("Invalid LLM pricing configuration.")
        self.model = model
        self.name = name
        self._api_key = api_key
        self._base_url = base_url
        self._pricing = dict(pricing)
        self._output_cap_parameter = output_cap_parameter
        self._pricing_version = pricing_version

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: CallOptions,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Reserve before every SDK invocation and stop before unaccounted use."""
        options.check()
        return asyncio.run(self._complete(messages, options=options, json_schema=json_schema))

    @staticmethod
    def _finish(options: CallOptions, attempt_id: int, **values: Any) -> None:
        try:
            options.observer.finish(attempt_id, **values)
        except BudgetStop:
            raise
        except Exception:
            raise BudgetStop("budget_unavailable") from None

    @staticmethod
    async def _backoff(options: CallOptions, delay: float) -> None:
        options.check()
        remaining = options.remaining_seconds()
        if remaining <= 0:
            raise BudgetStop("time_limit")
        try:
            async with asyncio.timeout(remaining):
                await asyncio.sleep(min(delay, remaining))
        except TimeoutError:
            raise BudgetStop("time_limit") from None
        options.check()

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: CallOptions,
        json_schema: dict[str, Any] | None,
    ) -> LLMResult:
        from openai import APITimeoutError, AsyncOpenAI, BadRequestError, RateLimitError

        msgs = list(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            self._output_cap_parameter: options.max_completion_tokens,
        }
        if json_schema is not None:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "Respond with a single JSON object that conforms to this "
                        "JSON Schema. No prose, no markdown fences, JSON only.\n"
                        + json.dumps(json_schema)
                    ),
                }
            )
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        group_id = str(uuid4())
        attempt_ids: list[int] = []
        rate_limit_retries = 0
        client = AsyncOpenAI(
            api_key=self._api_key, base_url=self._base_url, max_retries=0
        )
        try:
            while True:
                options.check()
                remaining = options.remaining_seconds()
                if remaining <= 0:
                    raise BudgetStop("time_limit")
                attempt_id = options.observer.begin(
                    call_group_id=group_id,
                    attempt_index=len(attempt_ids),
                    provider=self.name,
                    requested_model=self.model,
                    payload=payload,
                    pricing=self._pricing,
                    pricing_version=self._pricing_version,
                )
                attempt_ids.append(attempt_id)
                attempt_started = time.monotonic()
                response = None
                failure: Exception | None = None
                outcome = "success"
                try:
                    options.check()
                    timeout = min(options.attempt_timeout_seconds, options.remaining_seconds())
                    if timeout <= 0:
                        raise BudgetStop("time_limit")
                    async with asyncio.timeout(timeout):
                        response = await client.chat.completions.create(**payload)
                except RateLimitError as error:
                    failure, outcome = error, "rate_limit"
                except BadRequestError as error:
                    failure = error
                    outcome = (
                        "format_rejected"
                        if "response_format" in payload and _format_rejected(error)
                        else "error"
                    )
                except (TimeoutError, APITimeoutError) as error:
                    failure, outcome = error, "timeout"
                except BudgetStop:
                    self._finish(
                        options, attempt_id, returned_model=None, prompt_tokens=None,
                        completion_tokens=None,
                        latency_ms=int((time.monotonic() - attempt_started) * 1000),
                        outcome="timeout",
                    )
                    raise
                except Exception as error:
                    failure, outcome = error, "error"

                latency_ms = int((time.monotonic() - attempt_started) * 1000)
                if failure is not None:
                    if isinstance(failure, BadRequestError) and outcome == "error":
                        try:
                            options.check()
                        finally:
                            self._finish(
                                options, attempt_id, returned_model=None, prompt_tokens=None,
                                completion_tokens=None, latency_ms=latency_ms, outcome=outcome,
                            )
                        raise ValueError("LLM backend rejected configured request.") from None
                    self._finish(
                        options, attempt_id, returned_model=None, prompt_tokens=None,
                        completion_tokens=None, latency_ms=latency_ms, outcome=outcome,
                    )
                    options.check()
                    if outcome == "format_rejected":
                        payload.pop("response_format")
                        continue
                    if outcome == "rate_limit" and rate_limit_retries < RATE_LIMIT_MAX_RETRIES:
                        delay = _retry_delay_from(failure, rate_limit_retries)
                        rate_limit_retries += 1
                        await self._backoff(options, delay)
                        continue
                    raise BudgetStop("budget_unavailable") from None

                usage = getattr(response, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                returned_model = getattr(response, "model", None)
                if not isinstance(returned_model, str) or not returned_model:
                    returned_model = None
                usage_known = _valid_tokens(prompt_tokens) and _valid_tokens(completion_tokens)
                self._finish(
                    options, attempt_id, returned_model=returned_model,
                    prompt_tokens=prompt_tokens if _valid_tokens(prompt_tokens) else None,
                    completion_tokens=(
                        completion_tokens if _valid_tokens(completion_tokens) else None
                    ),
                    latency_ms=latency_ms, outcome="success",
                )
                options.check()
                if not usage_known or returned_model not in self._pricing:
                    raise BudgetStop("budget_unavailable")
                try:
                    choice = response.choices[0]
                    content = choice.message.content
                    finish_reason = choice.finish_reason
                except (AttributeError, IndexError, TypeError):
                    raise BudgetStop("budget_unavailable") from None
                parsed = None
                if json_schema is not None and content:
                    try:
                        data = json.loads(content)
                        if isinstance(data, dict):
                            parsed = data
                    except (json.JSONDecodeError, ValueError):
                        pass
                return LLMResult(
                    content=content, parsed=parsed, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=_compute_cost(
                        returned_model, prompt_tokens, completion_tokens, self._pricing
                    ),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    model=returned_model, extra={"finish_reason": finish_reason},
                    attempt_ids=tuple(attempt_ids), usage_known=True,
                )
        finally:
            try:
                await client.close()
            except Exception:
                raise BudgetStop("budget_unavailable") from None
