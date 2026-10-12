"""Cumulative turn limits and the sole writer of physical model accounting."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from laplace import repo
from laplace.config import Settings
from laplace.db import session_scope
from laplace.llm.base import BudgetStop, CallOptions

ESTIMATOR_VERSION = "utf8-json-v1"
_MILLION = Decimal(1_000_000)


def _rate(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BudgetStop("budget_unavailable") from exc
    if not result.is_finite() or result < 0:
        raise BudgetStop("budget_unavailable")
    return result


class TurnBudget:
    """One active clock, quota, error streak and conservative USD charge per request."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        settings = settings if settings is not None else Settings()
        self.max_steps = settings.max_steps
        self.max_seconds = settings.max_seconds
        self.max_cost_per_task = Decimal(str(settings.max_cost_per_task))
        self.max_completion_tokens = settings.llm_max_completion_tokens
        self.llm_timeout_seconds = settings.llm_timeout_seconds
        self.max_repeated_errors = settings.max_repeated_errors
        self._clock = clock
        self._cancel_check = cancel_check
        self._active_started: float | None = clock()
        self._elapsed = 0.0
        self.steps_used = 0
        self.budget_charge_usd = Decimal(0)
        self.known_cost_usd = Decimal(0)
        self.physical_attempts = 0
        self.unknown_attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.logical_calls = 0
        self._groups: set[str] = set()
        self.stop_reason: str | None = None
        self.overshoot = False
        self._error_fingerprint: tuple[str, str] | None = None
        self._error_count = 0

    def active_seconds(self) -> float:
        live = 0.0 if self._active_started is None else self._clock() - self._active_started
        return self._elapsed + max(0.0, live)

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.active_seconds())

    def check(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise BudgetStop("cancelled")
        if self.remaining_seconds() <= 0:
            raise BudgetStop("time_limit")
        if self.stop_reason is not None:
            raise BudgetStop(self.stop_reason)

    def pause(self) -> None:
        self.check()
        if self._active_started is not None:
            self._elapsed = self.active_seconds()
            self._active_started = None

    def resume(self) -> None:
        if self._active_started is None:
            self._active_started = self._clock()
        self.check()

    def admit_tool(self) -> None:
        self.check()
        if self.steps_used >= self.max_steps:
            raise BudgetStop("step_limit")
        self.steps_used += 1

    def observe_tool(self, tool_name: str, result: Any) -> None:
        if result.ok:
            self._error_fingerprint = None
            self._error_count = 0
            return
        fingerprint = (tool_name, result.error_code or "runtime_error")
        self._error_count = self._error_count + 1 if fingerprint == self._error_fingerprint else 1
        self._error_fingerprint = fingerprint
        if self._error_count >= self.max_repeated_errors:
            self.stop_reason = "repeated_error"

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "steps_used": self.steps_used,
            "steps_remaining": max(0, self.max_steps - self.steps_used),
            "active_seconds": round(self.active_seconds(), 3),
            "seconds_remaining": round(self.remaining_seconds(), 3),
            "cost_limit_usd": str(self.max_cost_per_task),
            "budget_charge_usd": str(self.budget_charge_usd),
            "known_cost_usd": str(self.known_cost_usd),
            "unknown_attempts": self.unknown_attempts,
            "physical_attempts": self.physical_attempts,
            "stop_reason": self.stop_reason,
            "overshoot": self.overshoot,
        }

    def call_options(self, observer: AttemptRecorder) -> CallOptions:
        self.check()
        return CallOptions(
            max_completion_tokens=self.max_completion_tokens,
            attempt_timeout_seconds=self.llm_timeout_seconds,
            remaining_seconds=self.remaining_seconds,
            check=self.check,
            observer=observer,
        )


@dataclass(frozen=True, slots=True)
class _Attempt:
    reserved: Decimal
    requested_model: str
    pricing: Mapping[str, tuple[float, float]]
    detail: dict[str, Any]


class AttemptRecorder:
    """Commit reservations before I/O, reconcile afterwards; never store request bodies."""

    def __init__(
        self,
        budget: TurnBudget,
        *,
        user_id: int | None,
        request_message_id: int | None,
        task_id: int | None = None,
        purpose: str = "agent",
        _tickets: dict[int, _Attempt] | None = None,
    ) -> None:
        self.budget = budget
        self.user_id = user_id
        self.request_message_id = request_message_id
        self.task_id = task_id
        self.purpose = purpose
        self._tickets = _tickets if _tickets is not None else {}

    def for_call(self, purpose: str, task_id: int | None = None) -> AttemptRecorder:
        return AttemptRecorder(
            self.budget, user_id=self.user_id, request_message_id=self.request_message_id,
            task_id=task_id, purpose=purpose, _tickets=self._tickets,
        )

    def _accounting_failure(self) -> None:
        self.budget.stop_reason = "budget_unavailable"
        self.budget.check()

    def begin(
        self,
        *,
        call_group_id: str,
        attempt_index: int,
        provider: str,
        requested_model: str,
        payload: dict[str, Any],
        pricing: Mapping[str, tuple[float, float]],
        pricing_version: str,
    ) -> int:
        self.budget.check()
        prices = pricing.get(requested_model)
        if prices is None:
            self._accounting_failure()
        assert prices is not None
        input_rate, output_rate = (_rate(value) for value in prices)
        caps = [payload[key] for key in ("max_tokens", "max_completion_tokens") if key in payload]
        if (
            len(caps) != 1 or type(caps[0]) is not int
            or not 1 <= caps[0] <= self.budget.max_completion_tokens
        ):
            self._accounting_failure()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        input_estimate = (
            len(serialized.encode("utf-8")) + 256 + 32 * len(payload.get("messages", []))
        )
        reserved = (
            Decimal(input_estimate) * input_rate
            + Decimal(caps[0]) * output_rate
        ) / _MILLION
        if self.budget.budget_charge_usd + reserved > self.budget.max_cost_per_task:
            raise BudgetStop("cost_limit")
        detail = {
            "requested_model": requested_model,
            "returned_model": None,
            "estimator_version": ESTIMATOR_VERSION,
            "pricing_version": pricing_version,
            "input_price_per_million": str(input_rate),
            "output_price_per_million": str(output_rate),
            "outcome": "reserved",
            "usage_known": False,
        }
        try:
            with session_scope(timeout_seconds=self.budget.remaining_seconds()) as session:
                row = repo.record_llm_call(
                    session, user_id=self.user_id, task_id=self.task_id,
                    request_message_id=self.request_message_id, purpose=self.purpose,
                    provider=provider, model=requested_model, prompt_tokens=0,
                    completion_tokens=0, cost_usd=0.0, latency_ms=0,
                    call_group_id=call_group_id, attempt_index=attempt_index,
                    accounting_status="reserved", budget_charge_usd=float(reserved),
                    accounting_detail=json.dumps(detail, separators=(",", ":")),
                )
                attempt_id = row.id
        except Exception:
            self._accounting_failure()
            raise AssertionError("unreachable")
        self._tickets[attempt_id] = _Attempt(reserved, requested_model, dict(pricing), detail)
        self.budget.budget_charge_usd += reserved
        self.budget.physical_attempts += 1
        self.budget.unknown_attempts += 1
        if call_group_id not in self.budget._groups:
            self.budget._groups.add(call_group_id)
            self.budget.logical_calls += 1
        return attempt_id

    def finish(
        self,
        attempt_id: int,
        *,
        returned_model: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        outcome: str,
    ) -> None:
        ticket = self._tickets.get(attempt_id)
        if ticket is None:
            self._accounting_failure()
        assert ticket is not None
        prices = ticket.pricing.get(returned_model) if returned_model is not None else None
        usage_known = (
            outcome == "success" and prices is not None
            and type(prompt_tokens) is int and prompt_tokens >= 0
            and type(completion_tokens) is int and completion_tokens >= 0
        )
        input_rate = output_rate = Decimal(0)
        if usage_known:
            try:
                input_rate, output_rate = (_rate(value) for value in prices)
            except BudgetStop:
                usage_known = False
        charge = ticket.reserved
        actual = Decimal(0)
        if usage_known:
            actual = (
                Decimal(prompt_tokens) * input_rate + Decimal(completion_tokens) * output_rate
            ) / _MILLION
            charge = actual
        detail = {
            **ticket.detail,
            "returned_model": returned_model,
            "outcome": outcome,
            "usage_known": usage_known,
        }
        if usage_known:
            detail.update(input_price_per_million=str(input_rate),
                          output_price_per_million=str(output_rate))
        try:
            with session_scope(timeout_seconds=self.budget.remaining_seconds()) as session:
                changed = repo.finalize_llm_attempt(
                    session, attempt_id, accounting_status="settled" if usage_known else "unknown",
                    model=returned_model or ticket.requested_model,
                    prompt_tokens=prompt_tokens if usage_known else 0,
                    completion_tokens=completion_tokens if usage_known else 0,
                    cost_usd=float(actual), budget_charge_usd=float(charge),
                    latency_ms=latency_ms,
                    accounting_detail=json.dumps(detail, separators=(",", ":")),
                )
                if not changed:
                    raise RuntimeError("Attempt reservation no longer belongs to this call.")
        except Exception:
            self._accounting_failure()
            raise AssertionError("unreachable")
        del self._tickets[attempt_id]
        self.budget.budget_charge_usd += charge - ticket.reserved
        if usage_known:
            self.budget.unknown_attempts -= 1
            self.budget.known_cost_usd += actual
            self.budget.prompt_tokens += prompt_tokens
            self.budget.completion_tokens += completion_tokens
            if (
                actual > ticket.reserved
                or self.budget.budget_charge_usd > self.budget.max_cost_per_task
            ):
                self.budget.overshoot = True
                self.budget.stop_reason = "cost_limit"
        elif outcome not in {"rate_limit", "format_rejected"}:
            self.budget.stop_reason = "budget_unavailable"
