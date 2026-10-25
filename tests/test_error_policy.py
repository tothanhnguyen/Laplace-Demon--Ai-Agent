"""Error taxonomy, gateway transient retry, and turn-level strategy."""

from datetime import UTC, datetime

import pytest

from laplace.config import Settings
from laplace.llm.base import BudgetStop
from laplace.services.budget import TurnBudget
from laplace.services.error_policy import Decision, decide, recovery_facts
from laplace.tools import base, errors
from laplace.tools.base import (
    PreparedProposal,
    RiskLevel,
    ToolContext,
    ToolFailure,
    ToolModel,
    ToolResult,
    execute,
    tool,
)
from laplace.tools.errors import ErrorClass, classify, classify_result, run_with_transient_retry


class _Args(ToolModel):
    value: str


class _Out(ToolModel):
    content: str


class _Prepared(ToolModel):
    model_config = {"extra": "forbid", "strict": True, "frozen": True}
    value: str


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(base, "_REGISTRY", {})
    monkeypatch.setattr(errors, "sleep", lambda _seconds: None)


def _context(max_seconds: float = 120, clock=None) -> ToolContext:
    budget = TurnBudget(
        Settings(_env_file=None, max_steps=20, max_seconds=max_seconds),
        **({"clock": clock} if clock else {}),
    )
    return ToolContext(
        budget=budget, user_id=1, telegram_user_id=10, transport="telegram", is_private_chat=True,
        conversation_id=1, request_message_id=1, task_id=1, step_index=0,
        now_utc=datetime(2026, 10, 20, tzinfo=UTC), timezone="UTC",
        authorized_effects=frozenset({"create_reminder"}),
    )


@pytest.mark.parametrize(
    ("code", "retryable", "expected"),
    [
        ("timeout", False, ErrorClass.TRANSIENT),
        ("timeout", True, ErrorClass.TRANSIENT),
        ("upstream_error", True, ErrorClass.TRANSIENT),
        ("upstream_error", False, ErrorClass.SERVICE),
        ("invalid_input", False, ErrorClass.PARAM),
        ("not_found", False, ErrorClass.PARAM),
        ("forbidden", False, ErrorClass.PERMISSION),
        ("forbidden", True, ErrorClass.PERMISSION),
        ("configuration_error", False, ErrorClass.SERVICE),
        ("unsupported_content", False, ErrorClass.SERVICE),
        ("runtime_error", False, ErrorClass.SERVICE),
        (None, False, ErrorClass.SERVICE),
    ],
)
def test_every_error_code_maps_to_exactly_one_class(code, retryable, expected) -> None:
    assert classify(code, retryable=retryable) is expected
    failed = ToolResult(
        ok=False, error="x", error_code=code or "runtime_error", retryable=retryable, latency_ms=0
    )
    assert classify_result(failed) is classify(code or "runtime_error", retryable=retryable)
    assert classify_result(ToolResult(ok=True, data=None, latency_ms=0)) is None


def test_decide_strategy_per_class() -> None:
    def failed(code, retryable=False):
        return ToolResult(ok=False, error="x", error_code=code, retryable=retryable, latency_ms=0)

    assert decide(ToolResult(ok=True, data=None, latency_ms=0), param_corrections_used=0) == (
        Decision("continue")
    )
    assert decide(failed("invalid_input"), param_corrections_used=0) == Decision(
        "correct_once", ErrorClass.PARAM
    )
    assert decide(failed("not_found"), param_corrections_used=1) == Decision(
        "stop", ErrorClass.PARAM, "param_error"
    )
    assert decide(failed("forbidden"), param_corrections_used=0) == Decision(
        "stop", ErrorClass.PERMISSION, "permission_required"
    )
    assert decide(failed("timeout"), param_corrections_used=0) == Decision(
        "stop", ErrorClass.TRANSIENT, "service_error"
    )
    assert decide(failed("runtime_error"), param_corrections_used=0) == Decision(
        "stop", ErrorClass.SERVICE, "service_error"
    )
    assert recovery_facts(1) == {"class": "param", "corrections_left": 0}


def test_gateway_retries_transient_read_failures_with_backoff_and_counts_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr(errors, "sleep", slept.append)
    calls = []

    @tool(name="flaky", purpose="p", when_to_use="w", when_not_to_use="n",
          risk_level=RiskLevel.READ, args_model=_Args, result_model=_Out)
    def flaky(args, context):
        calls.append(args.value)
        if len(calls) < 3:
            raise ToolFailure("timeout", "slow", retryable=True)
        return _Out(content="ok")

    context = _context()
    result = execute("flaky", {"value": "v"}, context)
    assert result.ok and result.attempts == 3 and len(calls) == 3
    assert slept == [0.5, 1.0]
    assert context.budget.steps_used == 1  # one admitted step despite three physical runs


def test_gateway_gives_up_after_two_retries_and_reports_attempts(monkeypatch):
    @tool(name="dead", purpose="p", when_to_use="w", when_not_to_use="n",
          risk_level=RiskLevel.READ, args_model=_Args, result_model=_Out)
    def dead(args, context):
        raise TimeoutError("raw socket timeout")

    result = execute("dead", {"value": "v"}, _context())
    assert not result.ok and result.error_code == "timeout" and result.retryable
    assert result.attempts == 3
    assert "raw socket" not in (result.error or "")


def test_gateway_does_not_retry_param_or_service_failures(monkeypatch):
    calls = []

    @tool(name="once", purpose="p", when_to_use="w", when_not_to_use="n",
          risk_level=RiskLevel.READ, args_model=_Args, result_model=_Out)
    def once(args, context):
        calls.append(args.value)
        raise ToolFailure("upstream_error", "permanent")

    result = execute("once", {"value": "v"}, _context())
    assert not result.ok and result.attempts == 1 and calls == ["v"]


def test_gateway_never_retries_approved_writes(monkeypatch):
    runs = []

    def prepare(args, context):
        return PreparedProposal(payload=_Prepared(value=args.value), preview=("fact",))

    @tool(name="create_reminder", purpose="p", when_to_use="w", when_not_to_use="n",
          risk_level=RiskLevel.EXTERNAL_SEND, args_model=_Args, result_model=_Out,
          prepare=prepare)
    def write(args, context):
        runs.append(args.value)
        raise TimeoutError("commit may have happened")

    context = ToolContext(
        budget=TurnBudget(Settings(_env_file=None, max_steps=20)), user_id=1,
        telegram_user_id=10, transport="telegram", is_private_chat=True, conversation_id=1,
        request_message_id=1, task_id=1, step_index=0,
        now_utc=datetime(2026, 10, 20, tzinfo=UTC), timezone="UTC",
        authorized_effects=frozenset({"create_reminder"}), approval_id=5, approval_token="tok",
    )
    result = execute("create_reminder", {"value": "v"}, context)
    assert not result.ok and result.error_code == "timeout" and result.attempts == 1
    assert runs == ["v"]


def test_retry_stops_when_backoff_cannot_fit_or_turn_is_cancelled(monkeypatch):
    now = [0.0]
    budget = TurnBudget(
        Settings(_env_file=None, max_steps=20, max_seconds=1), clock=lambda: now[0]
    )
    attempts = []

    def run():
        attempts.append(1)
        now[0] += 0.7  # 0.3s left: the 0.5s backoff no longer fits, so no retry
        raise ToolFailure("timeout", "slow")

    with pytest.raises(ToolFailure):
        run_with_transient_retry(
            run, is_transient_failure=lambda exc: True, budget=budget, max_retries=2
        )
    assert len(attempts) == 1

    cancelled = TurnBudget(Settings(_env_file=None, max_steps=20), cancel_check=lambda: True)
    attempts.clear()
    with pytest.raises(BudgetStop):
        run_with_transient_retry(
            run, is_transient_failure=lambda exc: True, budget=cancelled, max_retries=2
        )
    assert len(attempts) == 1
