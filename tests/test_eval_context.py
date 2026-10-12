"""Regression tests for Sprint 3 evaluator accounting."""

import json
import sqlite3
from copy import deepcopy

import pytest

from experiments.context_eval_tasks import EvalCase
from laplace import repo
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.base import BudgetStop, LLMResult
from laplace.llm.mock import MockLLM, account_mock_result
from laplace.models import Message, ToolCall, Trace
from laplace.services.budget import AttemptRecorder, TurnBudget
from scripts import eval_context
from scripts.eval_context import (
    ContentProbe,
    LimitedProvider,
    _run_case,
    _run_incomplete,
    _score,
    _write_reports,
    public_projection,
)


@pytest.fixture
def call_options():
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    budget = TurnBudget(Settings(_env_file=None))
    recorder = AttemptRecorder(budget, user_id=None, request_message_id=None)
    yield budget.call_options(recorder)
    reset_engine_for_tests(None)


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

    def complete(self, messages, *, options, json_schema=None):
        self.attempts += 1
        raise RuntimeError("provider unavailable")


def test_failed_logical_call_consumes_max_call_budget(call_options) -> None:
    delegate = _AlwaysFails()
    provider = LimitedProvider(delegate, max_calls=1, stop_after_usd=1)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        provider.complete([{"role": "user", "content": "first"}], options=call_options)
    with pytest.raises(RuntimeError, match="max_calls"):
        provider.complete([{"role": "user", "content": "second"}], options=call_options)

    assert provider.calls == 1
    assert delegate.attempts == 1


class _InvalidActionProvider:
    name = "mock"

    def complete(self, messages, *, options, json_schema=None):
        return account_mock_result(
            LLMResult(parsed={"action": "tool"}, model="invalid-fixture"),
            messages, options=options, json_schema=json_schema,
        )


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

    def complete(self, messages, *, options, json_schema=None):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("later provider failure")
        return account_mock_result(
            LLMResult(
                parsed={"action": "final", "final_answer": "first answer"},
                model="usage-fixture",
                prompt_tokens=3,
                completion_tokens=2,
            ),
            messages, options=options, json_schema=json_schema,
        )


def test_case_recovers_committed_usage_after_later_exception(tmp_path) -> None:
    case = EvalCase("usage", ("first", "second"), ("never-present",))

    result = _run_case(case, "sprint3", _SucceedsThenFails(), tmp_path, 7302)

    assert result["runtime_status"] == "error"
    assert result["usage"]["llm_calls"] == 1
    assert result["usage"]["physical_attempts"] == 1
    assert result["usage"]["total_tokens"] == 5
    assert result["usage"]["unknown_attempts"] == 0


def test_evaluator_classifies_recovery_final_from_completed_task(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPLACE_MAX_STEPS", "1")
    case = EvalCase(
        "recovery", ("TOOL:source.txt",), ("FACT_RECOVERY",),
        files={"source.txt": "FACT_RECOVERY"},
    )
    provider = MockLLM([
        {"action": "tool", "tool": "read_file", "params": {"path": "source.txt"}},
        {"action": "tool", "tool": "read_file", "params": {"path": "source.txt"}},
        {"action": "final", "final_answer": "FACT_RECOVERY"},
    ])

    result = _run_case(case, "sprint3", provider, tmp_path, 7303)

    assert result["runtime_status"] == "completed"
    assert result["stop_reason"] == "final_after_tool_budget"
    assert result["task_success"] is True
    assert result["tool_executions"] == 1


def test_evaluator_reports_unknown_usage_without_discarding_durable_attempt(tmp_path):
    case = EvalCase("unknown", ("question?",), ("UNTRUSTED_ANSWER",))
    provider = MockLLM([
        LLMResult(
            parsed={"action": "final", "final_answer": "UNTRUSTED_ANSWER"},
            model="unknown-usage-fixture", extra={"usage_known": False},
        ),
    ])

    result = _run_case(case, "sprint3", provider, tmp_path, 7304)

    assert result["runtime_status"] != "completed"
    assert result["task_success"] is False
    assert result["usage"]["unknown_attempts"] == 1
    assert result["usage"]["physical_attempts"] == 1
    assert result["usage"]["llm_calls"] == 1
    assert result["usage"]["known_cost_usd"] == 0
    assert result["usage_provenance"] == "synthetic_mock_attempts"


def test_evaluator_503_backoff_cannot_restart_after_turn_deadline(monkeypatch):
    now = [0.0]
    budget = TurnBudget(Settings(_env_file=None, max_seconds=1), clock=lambda: now[0])
    recorder = AttemptRecorder(budget, user_id=None, request_message_id=None)

    class Unavailable:
        name = "live-fixture"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, options, json_schema=None):
            self.calls += 1
            raise RuntimeError("503 UNAVAILABLE")

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr("scripts.eval_context.time.sleep", sleep)
    delegate = Unavailable()
    provider = LimitedProvider(delegate, max_calls=2, stop_after_usd=1)

    with pytest.raises(BudgetStop) as stopped:
        provider.complete([], options=budget.call_options(recorder))

    assert stopped.value.reason == "time_limit"
    assert delegate.calls == 1
    assert provider.calls == 1
    assert provider.transient_retries == 1


def test_public_export_omits_payloads_and_pseudonymizes_sensitive_references(tmp_path):
    secret = "CANARY_EVAL_PRIVATE_7fc7"
    email = "owner@example.test"
    source = f"https://public.example/evidence?token={secret}&email={email}"
    case_id = f"/Users/private/{email}/{secret}"
    row = {
        "case_id": case_id,
        "rubric_id": case_id,
        "strategy": "sprint3",
        "answer": f"{secret} {email} +84987654321",
        "prompt": secret,
        "error": f"RuntimeError: Bearer {secret}",
        "runtime_status": "error",
        "stop_reason": f"provider error: {source}",
        "task_success": False,
        "expected": {source: True, email: False},
        "associations": {f"{email} => {source}": True},
        "forbidden": {secret: False},
        "evidence_available_last_call": {source: True},
        "logical_calls": 2,
        "sum_application_chars": 200,
        "peak_application_chars": 100,
        "usage": {
            "physical_attempts": 2, "total_tokens": 50, "known_cost_usd": 0.01,
            "budget_charge_usd": 0.02, "unknown_attempts": 0, "raw_result": secret,
        },
        "calls_by_purpose": {"agent_action": 2, email: 900},
        "calls": [{
            "purpose": "agent_action", "char_count": 100, "dropped_group_count": 0,
            "evidence_available": {source: True}, "messages": [{"content": secret}],
        }],
        "user_file": f"/Users/private/{secret}.txt",
        "database": f"/Users/private/{secret}.sqlite",
    }
    manifest = {
        "mode": "live", "models": [f"model-{email}-{secret}"],
        "requested_provider": secret, "resolved_provider": "gemini",
        "requested_cases": [case_id], "results": [row], email: secret,
    }
    original = deepcopy(manifest)

    projected = public_projection(manifest)
    report = _write_reports(tmp_path, manifest)
    exported = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    serialized = json.dumps(exported) + report

    for private_value in (secret, email, source, case_id, "+84987654321", "/Users/private"):
        assert private_value not in serialized
    assert "answer" not in exported["results"][0]
    assert "prompt" not in exported["results"][0]
    assert "database" not in exported["results"][0]
    assert "messages" not in exported["results"][0]["calls"][0]
    assert exported["results"][0]["usage"] == {
        "physical_attempts": 2, "total_tokens": 50, "known_cost_usd": 0.01,
        "budget_charge_usd": 0.02, "unknown_attempts": 0,
    }
    assert exported["results"][0]["calls_by_purpose"] == {"agent_action": 2}
    public_row = projected["results"][0]
    source_key = next(key for key, present in public_row["expected"].items() if present)
    assert source_key.startswith("evidence-sha256-")
    assert public_row["evidence_available_last_call"] == {source_key: True}
    assert public_row["calls"][0]["evidence_available"] == {source_key: True}
    assert list(public_row["expected"].values()) == [True, False]
    assert public_row["error"] == "unknown_error"
    assert public_row["stop_reason"] == "unknown"
    assert projected["models"][0].startswith("model-sha256-")
    assert projected["requested_cases"] == [public_row["case_id"]]
    assert public_projection(projected) == projected
    assert manifest == original
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == report


def test_public_metrics_reject_nonfinite_numbers_and_boolean_counters():
    projected = public_projection({
        "elapsed_ms": float("nan"), "cost_usd": float("inf"),
        "logical_calls": True, "physical_attempts": -1,
        "task_success": 1, "schema_version": 3,
    })

    assert projected == {
        "elapsed_ms": None, "cost_usd": None, "logical_calls": None,
        "physical_attempts": None, "task_success": None, "schema_version": 3,
    }
    assert json.loads(json.dumps(projected, allow_nan=False)) == projected


def test_case_exception_uses_fixed_code_without_raw_error(tmp_path, monkeypatch):
    secret = "CANARY_ERROR_RAW_d6b8"

    def fail(*args, **kwargs):
        raise RuntimeError(f"Bearer {secret}; owner@example.test; /Users/private/error.txt")

    monkeypatch.setattr(eval_context, "_run_reserved_message", fail)
    case = EvalCase("exception", ("question?",), ("EXPECTED",))

    result = _run_case(case, "sprint3", ContentProbe(), tmp_path, 7305)

    assert result["error"] == "RuntimeError"
    assert result["stop_reason"] == "RuntimeError"
    assert result["runtime_status"] == "error"
    assert result["task_success"] is False
    assert secret not in json.dumps(result)
    assert public_projection(result)["error"] == "RuntimeError"


def test_trace_boundary_preserves_private_canonical_and_historical_data(call_options):
    content = "CANARY_CANONICAL_20ca owner@example.test +84987654321"
    source = "https://public.example/document?token=CANARY_CANONICAL_20ca"
    params = json.dumps({"path": "/Users/private/owner-document.txt"})
    full_result = json.dumps({"text": content, "sources": [{"source": source}]})
    payload = {
        "event": "tool_call", "tool": "read_document", "ok": True,
        "latency_ms": 12, "error_code": "timeout",
        "answer": content, "params": params, "result_json": full_result,
        "telegram_user_id": 7355, "username": "owner@example.test",
    }
    original = deepcopy(payload)
    with session_scope() as session:
        user = repo.get_or_create_user(session, 7355, "owner@example.test")
        conversation = repo.get_or_create_conversation(session, user)
        message = repo.add_message(session, conversation, "user", content)
        task = repo.create_task(
            session, user_id=user.id, conversation_id=conversation.id,
            request_message_id=message.id, goal=content,
        )
        tool = repo.record_tool_call(
            session, task_id=task.id, tool_name="read_document", params_json=params,
            result_json=full_result, ok=True, error=None, latency_ms=12,
        )
        historical = Trace(
            task_id=task.id, conversation_id=conversation.id, step_index=0,
            payload_json=json.dumps({"event": "legacy", "answer": content}),
        )
        session.add(historical)
        session.flush()
        historical_payload = historical.payload_json
        trace = repo.record_trace(
            session, task_id=task.id, conversation_id=conversation.id,
            step_index=1, payload=payload,
        )
        persisted = json.loads(trace.payload_json)
        assert persisted["event"] == "tool_call"
        assert persisted["tool_name"] == "read_document"
        assert persisted["ok"] is True
        assert persisted["latency_ms"] == 12
        assert persisted["task_id"] == task.id
        assert persisted["step_index"] == 1
        assert "CANARY_CANONICAL_20ca" not in trace.payload_json
        assert "owner@example.test" not in trace.payload_json
        assert "telegram_user_id" not in persisted
        session.expire_all()
        assert session.get(Message, message.id).content == content
        canonical = session.get(ToolCall, tool.id)
        assert canonical.params_json == params
        assert canonical.result_json == full_result
        assert session.get(Trace, historical.id).payload_json == historical_payload
    assert payload == original


@pytest.mark.parametrize("keep_private", [False, True])
def test_evaluator_public_output_never_contains_private_sqlite_or_user_files(
    tmp_path, monkeypatch, capsys, keep_private,
):
    secret = "CANARY_PRIVATE_REPLAY_4d7a owner@example.test"
    case = EvalCase(
        "E-01", (f"Remember {secret}",), (secret,),
        files={"owner-document.txt": secret},
    )
    provider = MockLLM([
        {"action": "final", "final_answer": secret},
    ])
    historical = tmp_path / "experiments" / "results" / "historical" / "results.json"
    historical.parent.mkdir(parents=True)
    historical.write_text('{"historical": "unchanged"}', encoding="utf-8")
    historical_bytes = historical.read_bytes()
    monkeypatch.setattr(eval_context, "ROOT", tmp_path)
    monkeypatch.setattr(eval_context, "CONTEXT_EVAL_CASES", (case,))
    monkeypatch.setattr(eval_context, "ContentProbe", lambda: provider)
    monkeypatch.setattr(eval_context, "_code_revision", lambda: None)
    monkeypatch.setattr(eval_context, "_working_tree_dirty", lambda: None)
    monkeypatch.setattr(eval_context, "_source_hash", lambda: "a" * 64)
    args = ["eval_context.py", "--mode", "offline", "--strategies", "sprint3"]
    if keep_private:
        args.append("--keep-private-artifacts")
    monkeypatch.setattr("sys.argv", args)

    eval_context.main()

    exports = [
        path for path in historical.parent.parent.iterdir() if path != historical.parent
    ]
    assert len(exports) == 1
    public_root = exports[0]
    assert {path.name for path in public_root.iterdir()} == {"results.json", "report.md"}
    public_json = json.loads((public_root / "results.json").read_text(encoding="utf-8"))
    assert public_json["results"][0]["task_success"] is True
    assert public_json["results"][0]["runtime_status"] == "completed"
    public_text = "".join(
        path.read_text(encoding="utf-8") for path in public_root.iterdir()
    ) + capsys.readouterr().out
    assert "CANARY_PRIVATE_REPLAY_4d7a" not in public_text
    assert "owner@example.test" not in public_text
    assert "owner-document.txt" not in public_text
    assert ".sqlite" not in public_text
    assert not list(public_root.rglob("*.sqlite"))
    assert historical.read_bytes() == historical_bytes
    private_parent = tmp_path / "var" / "eval_context"
    if not keep_private:
        assert not private_parent.exists()
        return
    private_root = private_parent / public_root.name
    private_manifest = json.loads(
        (private_root / "private-results.json").read_text(encoding="utf-8")
    )
    assert private_manifest["results"][0]["answer"] == secret
    assert private_manifest["results"][0]["expected"] == {secret: True}
    database = private_root / "repeat-0" / "E-01-sprint3" / "eval.sqlite"
    assert database.is_file()
    assert (database.parent / "owner-document.txt").read_text(encoding="utf-8") == secret
    with sqlite3.connect(database) as connection:
        contents = [row[0] for row in connection.execute("SELECT content FROM messages")]
    assert secret in contents
    assert f"Remember {secret}" in contents
    assert private_root.stat().st_mode & 0o777 == 0o700
    assert database.stat().st_mode & 0o777 == 0o600
