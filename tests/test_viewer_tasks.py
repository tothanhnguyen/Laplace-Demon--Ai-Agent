"""Observation pages: task list, detail timeline, 404, redaction, filters, loopback binding."""

import inspect

import pytest
from fastapi.testclient import TestClient

from laplace.config import Settings
from laplace.db import reset_engine_for_tests, session_scope
from laplace.evals.cases import EvalCase
from laplace.evals.runner import run_suite
from laplace.models import LLMCall
from laplace.services import approvals, chat, memory
from laplace.viewer import __main__ as viewer_main
from laplace.viewer import common
from laplace.viewer.app import create_app

SECRET = "tvly-secret-value-1234567890"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    monkeypatch.setattr(common, "configured_secrets", lambda: (SECRET,))
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    cases = [
        EvalCase.model_validate({
            "id": "n01-web", "group": "normal", "description": "d",
            "input": {"text": f"Tìm trên web: token {SECRET} là gì?"},
            "mock_script": [
                {"action": "tool", "tool": "web_search", "params": {"query": "x"}},
                {"action": "final", "final_answer": f"Giá trị là {SECRET}."},
            ],
            "expected": {"task_status": "completed"},
        }),
        EvalCase.model_validate({
            "id": "n02-chat", "group": "normal", "description": "d",
            "input": {"text": "chào"}, "mock_script": [{"action": "final", "final_answer": "hi"}],
            "expected": {"task_status": "none"},
        }),
    ]
    run_suite(cases, provider_mode="mock", run_dir=tmp_path / "r", run_id="v1")
    with session_scope() as session:  # a bot mid-turn: reserved accounting row
        session.add(LLMCall(purpose="agent_action", provider="mock", accounting_status="reserved"))
    app = create_app(f"sqlite:///{tmp_path / 'r' / 'laplace.db'}")
    yield TestClient(app)
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def test_list_and_detail_render_timeline_tools_and_verification(client) -> None:
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 307 and home.headers["location"] == "/tasks"
    page = client.get("/tasks")
    assert page.status_code == 200 and "Tác vụ (1)" in page.text and "/tasks/1" in page.text
    detail = client.get("/tasks/1")
    assert detail.status_code == 200
    for fragment in ("web_search", "Dòng thời gian", "Lời gọi công cụ (1)", "Kiểm chứng",
                     "Lời gọi mô hình (2)", "final"):
        assert fragment in detail.text, fragment


def test_secrets_are_redacted_everywhere(client) -> None:
    for path in ("/tasks", "/tasks/1"):
        text = client.get(path).text
        assert SECRET not in text and "Tìm trên web" in text


def test_404_filters_and_bad_dates(client) -> None:
    assert client.get("/tasks/999").status_code == 404
    assert client.get("/tasks?status=failed").text.count("/tasks/1") == 0
    assert client.get("/tasks?status=completed").text.count('href="/tasks/1"') == 1
    assert client.get("/tasks?from=2000-01-01&to=2000-01-02").text.count('href="/tasks/1"') == 0
    assert client.get("/tasks?from=nope").status_code == 400


def test_viewer_never_mutates_the_database_and_hides_openapi(client) -> None:
    with session_scope() as session:
        reserved = session.query(LLMCall).filter_by(accounting_status="reserved").count()
    assert reserved == 1  # create_app must not run init_db (which interrupts reserved rows)
    assert client.get("/openapi.json").status_code == 404
    with pytest.raises(FileNotFoundError):
        create_app("sqlite:////nonexistent/dir/laplace.db")


def test_viewer_binds_loopback_only(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(viewer_main.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(viewer_main, "create_app", lambda url: object())
    viewer_main.serve(port=9999)
    assert captured["host"] == "127.0.0.1" and captured["port"] == 9999
    assert "host" not in inspect.signature(viewer_main.serve).parameters
