"""Regression tests for Sprint 3 evaluator accounting."""

import pytest

from experiments.context_eval_tasks import EvalCase
from laplace.llm.base import LLMResult
from scripts.eval_context import (
    ContentProbe,
    LimitedProvider,
    _run_case,
    _run_incomplete,
    _score,
)


def test_scorer_normalizes_case_and_whitespace() -> None:
    case = EvalCase("score", (), ("FACT VALUE",), ("WRONG VALUE",))

    result = _score(case, "  fact\n value  ")

    assert result["task_success"] is True
    assert result["expected"] == {"FACT VALUE": True}
    assert result["forbidden"] == {"WRONG VALUE": False}


def test_scorer_rejects_reversed_source_attribution_and_negation() -> None:
    case = EvalCase(
        "association",
        (),
        ("FACT_A", "FACT_B"),
        required_associations=(("Nguồn A", "FACT_A"), ("Nguồn B", "FACT_B")),
    )

    reversed_answer = _score(
        case,
        "Nguồn A ghi FACT_B; Nguồn B ghi FACT_A.",
    )
    negated_answer = _score(
        EvalCase("negated", (), ("FACT_A",)),
        "Không phải FACT_A.",
    )

    assert reversed_answer["task_success"] is False
    assert negated_answer["task_success"] is False


def test_any_non_completed_row_marks_matrix_incomplete() -> None:
    assert (
        _run_incomplete(
            [{"runtime_status": "error", "error": "RateLimitError: quota"}],
            1,
            ContentProbe(),
        )
        is True
    )
    assert (
        _run_incomplete(
            [{"runtime_status": "failed", "error": None}],
            1,
            ContentProbe(),
        )
        is True
    )
    assert (
        _run_incomplete(
            [{"runtime_status": "completed", "error": None}],
            1,
            ContentProbe(),
        )
        is False
    )


class _AlwaysFails:
    name = "live-fixture"

    def __init__(self) -> None:
        self.attempts = 0

    def complete(self, messages, *, json_schema=None):
        self.attempts += 1
        raise RuntimeError("provider unavailable")


def test_failed_logical_call_consumes_max_call_budget() -> None:
    delegate = _AlwaysFails()
    provider = LimitedProvider(delegate, max_calls=1, stop_after_usd=1)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        provider.complete([{"role": "user", "content": "first"}])
    with pytest.raises(RuntimeError, match="max_calls"):
        provider.complete([{"role": "user", "content": "second"}])

    assert provider.calls == 1
    assert delegate.attempts == 1


class _InvalidActionProvider:
    name = "mock"

    def complete(self, messages, *, json_schema=None):
        return LLMResult(parsed={"action": "tool"}, model="invalid-fixture")


def test_case_reports_persisted_schema_failure_not_final_response(tmp_path) -> None:
    case = EvalCase("schema", ("question?",), ("EXPECTED",))

    result = _run_case(case, "sprint3", _InvalidActionProvider(), tmp_path, 7301)

    assert result["runtime_status"] == "failed"
    assert result["stop_reason"] == "schema_failure"
    assert result["task_success"] is False
    assert result["error"] is None


class _SucceedsThenFails:
    name = "live-fixture"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, json_schema=None):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("later provider failure")
        return LLMResult(
            parsed={"action": "final", "final_answer": "first answer"},
            model="usage-fixture",
            prompt_tokens=3,
            completion_tokens=2,
            cost_usd=0.01,
        )


def test_case_recovers_committed_usage_after_later_exception(tmp_path) -> None:
    case = EvalCase("usage", ("first", "second"), ("never-present",))

    result = _run_case(case, "sprint3", _SucceedsThenFails(), tmp_path, 7302)

    assert result["runtime_status"] == "error"
    assert result["usage"] == {
        "llm_calls": 1,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "cost_usd": 0.01,
    }
