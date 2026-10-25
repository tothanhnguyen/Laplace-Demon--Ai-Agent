"""F01–F09: inject faults into production tools and observe recovery through the runtime.

Each case asserts behaviour and persisted state (ToolCall/Task/Reminder/Approval),
never model wording. F10 (process kill) lives in test_restart_recovery.
"""

import json

import pytest
from sqlalchemy import select

from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import Reminder, Step, Task, ToolApproval, ToolCall
from laplace.services import approvals, chat, memory
from laplace.tools import base as tool_base
from laplace.tools.base import ToolFailure, ToolPayload

URL = "https://example.test/fault"
PAGE = "Dữ liệu tổng hợp cho kiểm thử phục hồi lỗi."


@pytest.fixture(autouse=True)
def _runtime(monkeypatch, tmp_path):
    reset_engine_for_tests(f"sqlite:///{tmp_path / 'fault.sqlite'}")
    init_db()
    memory.start_chat_admission()
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    from laplace.tools import reminders as reminder_tools

    monkeypatch.setattr(
        reminder_tools, "Settings",
        lambda: Settings(_env_file=None, reminder_max_horizon_days=365),
    )
    yield
    chat.interrupt_paused_turns()
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def _model(monkeypatch, script):
    provider = MockLLM(script=script)
    monkeypatch.setattr(chat, "get_provider", lambda: provider)
    return provider


def _web_ok(args, _context):
    return ToolPayload(
        data={"query": args.query, "results": [{"title": "T", "url": URL, "content": PAGE}]},
        sources=(URL,),
    )


def _page_ok(args, _context):
    return ToolPayload(
        data={"query": args.query, "results": [{"url": URL, "content": PAGE}]}, sources=(URL,)
    )


def _document_ok(args, _context):
    return ToolPayload(
        data={
            "document": {
                "attachment_id": args.attachment_id, "filename": "synthetic.txt",
                "format": "txt", "size_bytes": 10, "unit_kind": "line", "total_units": 1,
            },
            "units": [{"unit": 1, "locator": "line:1", "text": PAGE}],
            "next_unit": None, "complete": True, "limitations": [],
        },
        sources=(f"attachment:{args.attachment_id}#line:1",),
    )


def _search(query="kiểm thử"):
    return {"action": "tool", "tool": "web_search", "params": {"query": query}}


def _fetch():
    return {"action": "tool", "tool": "fetch_page", "params": {"url": URL, "query": "kiểm thử"}}


def _read(attachment_id="doc-1"):
    return {"action": "tool", "tool": "read_document", "params": {"attachment_id": attachment_id}}


def _final(text="Xong."):
    return {"action": "final", "final_answer": text}


def _state():
    with session_scope() as session:
        task = session.scalar(select(Task))
        calls = list(session.scalars(select(ToolCall).order_by(ToolCall.id)))
        last_step = session.scalars(select(Step).order_by(Step.id.desc())).first()
        return {
            "status": task.status if task else None,
            "verification": task.verification_status if task else None,
            "calls": [
                (c.tool_name, c.ok, json.loads(c.result_json).get("attempts")) for c in calls
            ],
            "stop": json.loads(last_step.detail)["stop_reason"] if last_step else None,
            "reminders": len(list(session.scalars(select(Reminder)))),
        }


def test_f01_web_search_recovers_from_two_timeouts_in_one_step(monkeypatch, faulty_tool):
    tool = faulty_tool("web_search", [TimeoutError(), TimeoutError(), _web_ok])
    provider = _model(monkeypatch, [_search(), _final("Có kết quả.")])

    reply = chat.handle_message(8101, "f01", "tìm thử")

    assert tool.calls == 3 and tool.slept == [0.5, 1.0]
    state = _state()
    assert state["status"] == "completed" and state["calls"] == [("web_search", True, 3)]
    assert state["stop"] == "final" and len(provider.calls) == 2
    assert "Căn cứ đã kiểm tra — Kiểm chứng một phần" in reply.text  # no claims supplied


def test_f02_fetch_page_exhausted_timeouts_stop_as_service_error(monkeypatch, faulty_tool):
    tool = faulty_tool("fetch_page", [TimeoutError()])
    provider = _model(monkeypatch, [_fetch(), _fetch(), _final("Không lấy được trang.")])

    reply = chat.handle_message(8102, "f02", "đọc trang")

    assert tool.calls == 3  # 1 + 2 retries, then no further tool proposal is accepted
    state = _state()
    assert state["status"] == "failed" and state["stop"] == "service_error"
    assert state["calls"] == [("fetch_page", False, 3)]
    assert len(provider.calls) == 3  # action, rejected tool proposal, final-only explanation
    assert "Lượt dừng sớm: lỗi dịch vụ." in reply.text


def test_f03_read_document_param_error_is_corrected_once(monkeypatch, faulty_tool):
    tool = faulty_tool(
        "read_document", [ToolFailure("invalid_input", "start_unit is beyond"), _document_ok]
    )
    provider = _model(monkeypatch, [_read("doc-1"), _read("doc-1"), _final("Đã đọc.")])

    chat.handle_message(8103, "f03", "đọc tài liệu")

    assert tool.calls == 2
    state = _state()
    assert state["status"] == "completed"
    assert state["calls"] == [("read_document", False, 1), ("read_document", True, 1)]
    facts = json.loads(
        provider.calls[1]["messages"][0]["content"].split("TRUSTED_RUNTIME_FACTS_JSON:\n", 1)[1]
    )
    assert facts["recovery"] == {"class": "param", "corrections_left": 0}


def test_f04_two_param_errors_stop_with_param_error(monkeypatch, faulty_tool):
    tool = faulty_tool("web_search", [ToolFailure("invalid_input", "bad query")])
    provider = _model(monkeypatch, [_search(), _search("khác"), _final("must not run")])

    reply = chat.handle_message(8104, "f04", "tìm thử")

    assert tool.calls == 2 and len(provider.calls) == 2
    state = _state()
    assert state["status"] == "failed" and state["stop"] == "param_error"
    assert [ok for _, ok, _ in state["calls"]] == [False, False]
    assert "tham số" in reply.text


def test_f05_forbidden_stops_without_another_model_call(monkeypatch, faulty_tool):
    faulty_tool("web_search", [ToolFailure("forbidden", "denied")])
    provider = _model(monkeypatch, [_search(), _final("must not run")])

    reply = chat.handle_message(8105, "f05", "tìm thử")

    assert len(provider.calls) == 1
    state = _state()
    assert state["status"] == "failed" and state["stop"] == "permission_required"
    assert "/remind" in reply.text


def test_f06_raw_exception_is_a_service_error_without_leaking_its_message(
    monkeypatch, faulty_tool
):
    faulty_tool("read_document", [RuntimeError("secret /Users/private/file.txt")])
    _model(monkeypatch, [_read(), _final("Không đọc được tệp.")])

    reply = chat.handle_message(8106, "f06", "đọc tài liệu")

    state = _state()
    assert state["status"] == "failed" and state["stop"] == "service_error"
    assert state["calls"] == [("read_document", False, 1)]
    assert "/Users/private" not in reply.text
    with session_scope() as session:
        assert "/Users/private" not in session.scalar(select(ToolCall)).result_json


def _approved_reminder(monkeypatch, telegram_user_id: int):
    provider = _model(monkeypatch, [
        {"action": "tool", "tool": "create_reminder",
         "params": {"message": "fault reminder", "delay_seconds": 300}},
        _final("Đã tạo."),
    ])
    lease = memory.admit_chat_turn(telegram_user_id)
    reply = chat._run_reserved_message(
        lease, telegram_user_id, "owner", "/remind nhắc tôi sau 5 phút", chat_id=telegram_user_id
    )
    pause = reply.pause
    assert pause is not None
    assert approvals.bind_preview(
        pause.token, telegram_user_id=telegram_user_id, chat_id=pause.chat_id, message_id=5
    )
    lease = memory.try_reserve_turn(telegram_user_id, kind="chat")
    record = approvals.claim_approval(
        pause.token, telegram_user_id=telegram_user_id, chat_id=pause.chat_id, message_id=5
    )
    assert record is not None
    return pause, lease, provider


def test_f07_approved_write_timeout_runs_once_and_commits_nothing(monkeypatch, faulty_tool):
    pause, lease, provider = _approved_reminder(monkeypatch, 8107)
    tool = faulty_tool("create_reminder", [TimeoutError()])

    reply = chat.resume_approved(lease, pause.token, provider=provider)

    assert tool.calls == 1 and tool.slept == []
    with session_scope() as session:
        assert session.get(ToolApproval, pause.approval_id).status == "failed"
        assert session.scalar(select(Reminder)) is None
        assert session.get(Task, pause.task_id).status == "failed"
    assert "Căn cứ đã kiểm tra — Không áp dụng" in reply.text
    assert "thao tác đã duyệt thất bại" in reply.text


def test_f08_commit_then_crash_recovers_the_receipt_without_a_duplicate_row(
    monkeypatch, faulty_tool
):
    pause, lease, provider = _approved_reminder(monkeypatch, 8108)
    real_run = tool_base.get_tool("create_reminder").run

    def commit_then_raise(args, context):
        real_run(args, context)
        raise RuntimeError("connection dropped after commit")

    tool = faulty_tool("create_reminder", [commit_then_raise])

    reply = chat.resume_approved(lease, pause.token, provider=provider)

    assert tool.calls == 1
    with session_scope() as session:
        reminders = list(session.scalars(select(Reminder)))
        assert len(reminders) == 1 and reminders[0].message == "fault reminder"
        assert session.get(ToolApproval, pause.approval_id).status == "executed"
        task = session.get(Task, pause.task_id)
        assert task.verification_status == "verified" and task.status == "completed"
        calls = list(session.scalars(select(ToolCall)))
        assert len(calls) == 1 and calls[0].approval_id == pause.approval_id and calls[0].ok
    assert f"- Đạt: Reminder #{reminders[0].id} có trong hệ thống (pending, " in reply.text
    assert "không thể hoàn tất" not in reply.text


def test_f09_cancel_during_backoff_stops_retrying(monkeypatch, faulty_tool):
    from laplace.tools import errors

    tool = faulty_tool("web_search", [TimeoutError()])
    monkeypatch.setattr(errors, "sleep", lambda _seconds: memory.request_cancel(8109))
    provider = _model(monkeypatch, [_search(), _final("must not run")])

    chat.handle_message(8109, "f09", "tìm thử")

    assert tool.calls == 1 and len(provider.calls) == 1
    state = _state()
    assert state["status"] == "cancelled" and state["stop"] == "cancelled"
    assert state["calls"] == []  # the cancelled attempt never produced an envelope


def test_registry_is_restored_between_tests():
    tool_base.load_builtin_tools()
    spec = tool_base.get_tool("web_search")
    assert spec is not None and spec.run.__name__ == "web_search"
