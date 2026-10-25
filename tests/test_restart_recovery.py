"""Startup recovery: abandoned tasks become interrupted, owners are told, nothing replays."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from laplace import repo
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import Message, Reminder, Step, Task, ToolApproval, Trace
from laplace.services import approvals, chat, memory, restart_recovery
from laplace.services.restart_recovery import recover_after_restart
from laplace.tools.base import ToolResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    memory.start_chat_admission()
    monkeypatch.setattr(
        chat, "get_provider", lambda: pytest.fail("recovery must not call the model")
    )
    yield
    chat.interrupt_paused_turns()
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def _task(telegram_user_id: int, *, status: str = "running", goal: str = "tìm web") -> dict:
    with session_scope() as session:
        user = repo.get_or_create_user(session, telegram_user_id, None)
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(session, conversation, "user", goal)
        task = repo.create_task(
            session, user_id=user.id, conversation_id=conversation.id,
            request_message_id=request.id, goal=goal,
        )
        task.status = status
        session.flush()
        return {"task_id": task.id, "user_id": user.id, "conversation_id": conversation.id}


def _executed_receipt(scope: dict) -> int:
    """Committed reminder plus its executed approval, as the approved tool leaves them."""
    with session_scope() as session:
        from laplace.services.reminders import public_reminder

        reminder = repo.create_reminder(
            session, user_id=scope["user_id"], message="nhắc sau restart", timezone="UTC",
            due_at_utc=1_900_000_000,
        )
        receipt = ToolResult(ok=True, data=public_reminder(reminder), latency_ms=1)
        session.add(ToolApproval(
            token="tok-executed", user_id=scope["user_id"], task_id=scope["task_id"],
            step_index=0, tool_name="create_reminder", risk_level="EXTERNAL_SEND",
            params_json="{}", public_args_hash="h", preview_json="{}", status="executed",
            created_at=1, expires_at=2, chat_id=1, result_json=receipt.model_dump_json(),
        ))
        return reminder.id


def _read_task(task_id: int) -> Task:
    with session_scope() as session:
        return session.get(Task, task_id)


def test_running_read_task_is_interrupted_with_a_notice_and_no_receipts() -> None:
    scope = _task(7001)
    result = recover_after_restart()

    assert result.marked == 1 and len(result.notices) == 1 and result.approvals == []
    notice = result.notices[0]
    assert notice.telegram_user_id == 7001 and notice.task_id == scope["task_id"]
    assert notice.text.startswith("Kết quả\nLượt xử lý trước bị gián đoạn do bot khởi động lại")
    assert "Căn cứ đã kiểm tra — Không áp dụng" in notice.text
    assert "Gửi lại yêu cầu nếu vẫn cần: tìm web" in notice.text
    assert notice.text.endswith("Hành động còn chờ duyệt\n- Không có")
    task = _read_task(scope["task_id"])
    assert task.status == "interrupted" and task.verification_status == "not_applicable"
    with session_scope() as session:
        step = session.scalar(select(Step).where(Step.task_id == scope["task_id"]))
        assert (step.name, step.status, step.step_index) == (
            "restart_interrupted", "interrupted", 0
        )
        events = [json.loads(row.payload_json) for row in session.scalars(select(Trace))]
        assert events[-1]["event"] == "restart_interrupted"
        stored = session.scalar(
            select(Message.content).where(Message.role == "assistant").order_by(Message.id.desc())
        )
        assert stored == notice.text.splitlines()[1]


def test_executed_receipt_is_rechecked_and_reported_to_the_owner() -> None:
    scope = _task(7002)
    reminder_id = _executed_receipt(scope)
    result = recover_after_restart()

    notice = result.notices[0]
    assert "Căn cứ đã kiểm tra — Đã kiểm chứng" in notice.text
    assert f"- Đạt: Reminder #{reminder_id} có trong hệ thống (pending, " in notice.text
    task = _read_task(scope["task_id"])
    assert task.status == "interrupted" and task.verification_status == "verified"
    with session_scope() as session:
        row = session.scalar(select(Reminder).where(Reminder.id == reminder_id))
        assert row.status == "pending"
        assert len(list(session.scalars(select(Reminder)))) == 1
        stored = session.scalar(
            select(Message.content).where(Message.role == "assistant").order_by(Message.id.desc())
        )
        assert stored.startswith(f"Thao tác đã thực thi:\n- Reminder #{reminder_id}")


def test_pending_approval_is_interrupted_and_keyboard_record_returned(monkeypatch, tmp_path):
    from laplace.config import Settings
    from laplace.tools import reminders as reminder_tools

    monkeypatch.setattr(
        reminder_tools, "Settings",
        lambda: Settings(_env_file=None, reminder_max_horizon_days=365),
    )
    provider = MockLLM([
        {"action": "tool", "tool": "create_reminder",
         "params": {"message": "đề xuất chờ", "delay_seconds": 180}},
        {"action": "final", "final_answer": "done"},
    ])
    monkeypatch.setattr(chat, "get_provider", lambda: provider)
    lease = memory.admit_chat_turn(7003)
    reply = chat._run_reserved_message(
        lease, 7003, "owner", "/remind xem lại sau 3 phút", chat_id=7003
    )
    assert reply.pause is not None
    monkeypatch.setattr(chat, "get_provider", lambda: pytest.fail("no model during recovery"))

    result = recover_after_restart()

    assert [record.status for record in result.approvals] == ["interrupted"]
    assert result.marked == 0  # the approval transition already moved the task
    assert len(result.notices) == 1
    assert "đề xuất chưa duyệt đã vô hiệu" in result.notices[0].text
    with session_scope() as session:
        assert session.get(Task, reply.pause.task_id).status == "interrupted"
        assert session.scalar(select(Reminder)) is None
        assert session.get(ToolApproval, reply.pause.approval_id).status == "interrupted"
    assert not chat.has_checkpoint(reply.pause.token, 7003)


def test_finished_tasks_are_untouched_and_second_run_is_a_noop() -> None:
    done = _task(7004, status="completed")
    failed = _task(7005, status="failed")
    active = _task(7006)
    first = recover_after_restart()
    second = recover_after_restart()

    assert [notice.task_id for notice in first.notices] == [active["task_id"]]
    assert second.notices == [] and second.marked == 0
    assert _read_task(done["task_id"]).status == "completed"
    assert _read_task(failed["task_id"]).status == "failed"
    with session_scope() as session:
        assert len(list(session.scalars(select(Step)))) == 1
        assert len(list(session.scalars(select(Message).where(Message.role == "assistant")))) == 1


def test_notice_bound_and_cli_user_still_marked(monkeypatch) -> None:
    monkeypatch.setattr(restart_recovery, "MAX_NOTICES", 2)
    scopes = [_task(7010 + index) for index in range(3)]
    cli = _task(0)
    result = recover_after_restart()

    assert result.marked == 4
    assert [notice.task_id for notice in result.notices] == [s["task_id"] for s in scopes[:2]]
    assert all(_read_task(s["task_id"]).status == "interrupted" for s in [*scopes, cli])


def test_notice_contains_no_private_paths_params_or_raw_errors(tmp_path) -> None:
    scope = _task(7020, goal="đọc file " + str(tmp_path / "secret.txt"))
    with session_scope() as session:
        failed = ToolResult(
            ok=False, error="Tool 'read_document' failed during execution: /Users/x",
            error_code="runtime_error", latency_ms=1,
        )
        repo.record_tool_call(
            session, task_id=scope["task_id"], tool_name="read_document",
            params_json=json.dumps({"attachment_id": "/private/path"}),
            result_json=failed.model_dump_json(), ok=False, error=failed.error, latency_ms=1,
        )
    notice = recover_after_restart().notices[0].text
    assert "/Users/x" not in notice and "/private/path" not in notice
    assert "Công cụ read_document lỗi: lỗi thực thi công cụ" in notice
    assert "failed during execution" not in notice


_CHILD = """
import sys
from laplace.db import init_db
from laplace.llm.mock import MockLLM
from laplace.services import chat, memory
from laplace.tools.base import RiskLevel, ToolModel, tool


class Args(ToolModel):
    value: str


class Out(ToolModel):
    content: str


@tool(name="blocker", purpose="p", when_to_use="w", when_not_to_use="n",
      risk_level=RiskLevel.READ, args_model=Args, result_model=Out)
def blocker(args, context):
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    sys.stdin.readline()  # blocks until the parent kills this process
    return Out(content="never")


init_db()
memory.start_chat_admission()
chat.get_provider = lambda: MockLLM([
    {"action": "tool", "tool": "blocker", "params": {"value": "x"}},
    {"action": "final", "final_answer": "never"},
])
chat.handle_message(9100, "kill-owner", "block the process")
"""


def test_killed_process_mid_task_is_recovered_as_interrupted_without_replay(tmp_path, monkeypatch):
    database = tmp_path / "crash.db"
    url = f"sqlite:///{database}"
    env = {
        **os.environ,
        "LAPLACE_DATABASE_URL": url,
        "LAPLACE_LLM_PROVIDER": "mock",
        "LAPLACE_TELEGRAM_BOT_TOKEN": "",
        "PYTHONPATH": str(ROOT),
    }
    env.pop("LAPLACE_ENV_FILE", None)
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, cwd=tmp_path, text=True,
    )
    watchdog = threading.Timer(90, process.kill)
    watchdog.start()
    try:
        ready = process.stdout.readline()
        assert ready.strip() == "READY", "child never reached the blocking tool"
        process.kill()
        process.wait(timeout=30)
    finally:
        watchdog.cancel()
        process.stdout.close()
        process.stdin.close()

    reset_engine_for_tests(url)
    init_db()
    with session_scope() as session:
        task = session.scalar(select(Task))
        assert task.status == "running"
    result = recover_after_restart()

    assert result.marked == 1 and [n.telegram_user_id for n in result.notices] == [9100]
    assert "- Đạt: Công cụ" not in result.notices[0].text  # the blocked call never recorded
    with session_scope() as session:
        assert session.scalar(select(Task)).status == "interrupted"
        assert session.scalar(select(Reminder)) is None
        assert approvals.epoch_now() > 0
    assert recover_after_restart().notices == []
