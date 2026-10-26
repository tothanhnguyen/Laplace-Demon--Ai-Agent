"""Harness runs cases through the real runtime and judges them from the database."""

import json

import pytest
from sqlalchemy import select

from laplace.config import Settings
from laplace.db import reset_engine_for_tests, session_scope
from laplace.evals import report
from laplace.evals.cases import EvalCase
from laplace.evals.runner import run_suite
from laplace.models import EvalResult, EvalRun, Reminder, ToolApproval
from laplace.services import approvals, chat, memory
from laplace.tools import base as tool_base


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    from laplace.tools import reminders as reminder_tools

    monkeypatch.setattr(
        reminder_tools, "Settings", lambda: Settings(_env_file=None, reminder_max_horizon_days=365)
    )
    yield
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def _case(id, **overrides) -> EvalCase:
    base = {"id": id, "group": "normal", "description": "d", "input": {"text": "xin chào"},
            "mock_script": [{"action": "final", "final_answer": "Chào bạn."}]}
    return EvalCase.model_validate({**base, **overrides})


def _remind_case(id, approval, expected, **overrides) -> EvalCase:
    return _case(
        id, group="approval", input={"text": "/remind nhắc tôi họp sau 10 phút"},
        mock_script=[
            {"action": "tool", "tool": "create_reminder",
             "params": {"message": "họp", "delay_seconds": 600}},
            {"action": "final", "final_answer": "Đã đặt nhắc."},
        ],
        approval=approval, expected=expected, **overrides,
    )


def test_passing_and_forbidden_cases_are_judged_from_db(tmp_path) -> None:
    ok = _case("n01-ok", expected={"task_status": "none", "reply_contains": ["chào"]})
    web = _case(
        "n02-web", input={"text": "tìm thử"},
        mock_script=[{"action": "tool", "tool": "web_search", "params": {"query": "x"}},
                     {"action": "final", "final_answer": "xong"}],
        faults=[{"tool": "web_search", "script": ["timeout", "ok"]}],
        expected={"task_status": "completed"}, forbidden={"tools": ["web_search"]},
    )
    suite = run_suite([ok, web], provider_mode="mock", run_dir=tmp_path / "r", run_id="t1")
    assert [r.passed for r in suite.results] == [True, False]
    assert suite.results[1].failure_group == "forbidden_action"
    assert suite.results[1].task_id is not None and suite.results[1].stop_reason == "final"
    assert suite.results[0].user_id != suite.results[1].user_id


def test_approve_flow_executes_write_with_approval_and_reject_leaves_no_row(tmp_path) -> None:
    approve = _remind_case("a01-approve", "approve", {
        "task_status": "completed", "verification_status": ["verified"], "reminders": 1,
        "approval_status": "executed", "tool_ok": ["create_reminder"],
    })
    reject = _remind_case("a02-reject", "reject", {
        "task_status": ["rejected"], "reminders": 0,
        "approval_status": "rejected",
    })
    suite = run_suite([approve, reject], provider_mode="mock", run_dir=tmp_path / "r",
                      run_id="t2")
    assert [r.passed for r in suite.results] == [True, True], [
        [c.detail for c in r.checks if not c.ok] for r in suite.results
    ]
    assert suite.results[0].usage["llm_calls"] == 2  # whole turn, not just the paused half
    with session_scope() as session:
        assert len(list(session.scalars(select(Reminder)))) == 1
        statuses = sorted(a.status for a in session.scalars(select(ToolApproval)))
        assert statuses == ["executed", "rejected"]


def test_harness_errors_budget_and_registry_restore(tmp_path) -> None:
    unknown = _case("n03-unknown", faults=[{"tool": "web_search", "script": ["ok"]},
                                           {"tool": "no_such_tool", "script": ["ok"]}])
    placeholder = _case("n04-placeholder", input={"text": "/remind_cancel #$seeded_reminder_id"})
    loop = _case(
        "n05-loop", input={"text": "tìm"},
        mock_script=[{"action": "tool", "tool": "web_search", "params": {"query": "xx"}}] * 6,
        budget={"max_steps": 2}, expected={"task_status": "completed", "stop_reason": "final"},
    )
    suite = run_suite([unknown, placeholder, loop], provider_mode="mock",
                      run_dir=tmp_path / "r", run_id="t3")
    first, second, third = suite.results
    assert first.failure_group == "harness_error" and "no_such_tool" in first.harness_error
    assert second.failure_group == "harness_error" and "seeded_reminder" in second.harness_error
    assert third.stop_reason in ("step_limit", "final_after_tool_budget")
    assert third.failure_group in ("budget_stop", "expected_mismatch")
    assert tool_base.get_tool("web_search").run.__name__ == "web_search"
    with pytest.raises(FileExistsError):
        run_suite([loop], provider_mode="mock", run_dir=tmp_path / "r", run_id="t3")


def test_case_budget_composes_with_patched_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chat, "Settings", lambda: Settings(_env_file=None, timezone="UTC",
                                                           max_cost_per_task=7))
    seen = {}
    original = chat._run_reserved_message

    def spy(*args, **kwargs):
        seen["settings"] = chat.Settings()
        return original(*args, **kwargs)

    monkeypatch.setattr(chat, "_run_reserved_message", spy)
    case = _case("n06-budget", budget={"max_steps": 3}, expected={"task_status": "none"})
    run_suite([case], provider_mode="mock", run_dir=tmp_path / "r", run_id="t5")
    assert seen["settings"].max_steps == 3 and float(seen["settings"].max_cost_per_task) == 7
    assert chat.Settings().max_steps == 5  # restored


def test_report_files_and_rows_are_written(tmp_path) -> None:
    case = _case("n01-ok", expected={"task_status": "none"})
    suite = run_suite([case], provider_mode="mock", run_dir=tmp_path / "r", run_id="t4")
    results, markdown = report.write_files(suite, tmp_path / "r")
    data = json.loads(results.read_text())
    assert data["passed"] == 1 and data["cases"][0]["checks"]
    assert "1/1 passed" in markdown.read_text()
    run_pk = report.store(suite)
    with session_scope() as session:
        run = session.get(EvalRun, run_pk)
        rows = list(session.scalars(select(EvalResult).where(EvalResult.run_id == run_pk)))
        assert run.run_id == "t4" and run.total == 1 and rows[0].case_id == "n01-ok"
        assert json.loads(rows[0].checks_json)[0]["kind"] == "task_status"
