"""Stats and eval pages agree with direct SQL over the run database; CSV exports work."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from laplace.config import Settings
from laplace.db import reset_engine_for_tests, session_scope
from laplace.evals import report
from laplace.evals.cases import EvalCase
from laplace.evals.runner import run_suite
from laplace.models import LLMCall, Step, Task
from laplace.services import approvals, chat, memory
from laplace.viewer.app import create_app


def _case(id, text, script, expected):
    return EvalCase.model_validate({
        "id": id, "group": "normal", "description": "d", "input": {"text": text},
        "mock_script": script, "expected": expected,
    })


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    search = {"action": "tool", "tool": "web_search", "params": {"query": "x"}}
    final = {"action": "final", "final_answer": "xong"}
    cases = [
        _case("n01-a", "Tìm trên web: a", [search, final], {"task_status": "completed"}),
        _case("n02-b", "Tìm trên web: b", [search, final], {"task_status": "none"}),  # fails
        _case("n03-c", "chào", [final], {"task_status": "none"}),
    ]
    suite = run_suite(cases, provider_mode="mock", run_dir=tmp_path / "r", run_id="run-a")
    report.store(suite)
    yield TestClient(create_app())
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def test_stats_match_direct_sql_and_csv(client) -> None:
    with session_scope() as session:
        total = session.scalar(select(func.count(Task.id)))
        completed = session.scalar(
            select(func.count(Task.id)).where(Task.status == "completed")
        )
        steps = session.scalar(select(func.count(Step.id)))
        llm_calls = session.scalar(select(func.count(LLMCall.id)))  # incl. task-less calls
        assert session.scalar(
            select(func.count(LLMCall.id)).where(LLMCall.task_id.is_(None))
        ) >= 1
    page = client.get("/stats")
    assert page.status_code == 200
    assert f"<td>{total}</td>" in page.text
    assert f"{completed} ({completed / total * 100:.0f}%)" in page.text
    assert f"{steps / total:.2f}" in page.text and f"<td>{llm_calls}</td>" in page.text
    assert "web_search" in page.text
    csv_text = client.get("/stats.csv").text
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 1 and rows[0]["tasks"] == str(total)
    assert rows[0]["completed"] == str(completed)
    assert client.get("/stats?from=bad").status_code == 400
    assert "Chưa có dữ liệu" in client.get("/stats?from=2000-01-01&to=2000-01-02").text


def test_evals_list_detail_and_csv(client) -> None:
    listing = client.get("/evals")
    assert listing.status_code == 200 and "/evals/run-a" in listing.text and "2 / 3" in listing.text
    detail = client.get("/evals/run-a")
    assert detail.status_code == 200
    assert "expected_mismatch" in detail.text and 'href="/tasks/1"' in detail.text
    assert "n02-b" in detail.text and "task_status" in detail.text
    assert client.get("/evals/nope").status_code == 404
    rows = list(csv.DictReader(io.StringIO(client.get("/evals/run-a.csv").text)))
    assert [r["case_id"] for r in rows] == ["n01-a", "n02-b", "n03-c"]
    assert rows[1]["passed"] == "0" and "task_status" in rows[1]["failed_checks"]
    assert client.get("/evals/nope.csv").status_code == 404
