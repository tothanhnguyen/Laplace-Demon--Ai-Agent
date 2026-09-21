"""M-12: dispatch và privacy của lệnh /memory, /forget qua handler thật."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject
from sqlalchemy import select

from laplace import repo
from laplace.bot import handlers
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.models import ToolCall
from laplace.services.memory import release_turn, try_reserve_turn


class _FakeMessage:
    def __init__(self, chat_type: str = "private") -> None:
        self.chat = SimpleNamespace(type=chat_type)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def _call(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_database() -> None:
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_tests(None)


def test_memory_is_private_only_and_reports_empty_session() -> None:
    group = _FakeMessage(chat_type="supergroup")
    _call(handlers.cmd_memory(group, 6201))
    assert group.answers == ["Hãy mở chat riêng với bot để xem bộ nhớ phiên."]

    private = _FakeMessage()
    _call(handlers.cmd_memory(private, 6201))
    assert private.answers == ["Bộ nhớ phiên đang trống."]


def test_memory_text_shows_remaining_work_and_tool_source() -> None:
    text = handlers._memory_text(
        {
            "conversations": 1,
            "messages": 2,
            "tasks": 1,
            "tool_calls": 1,
            "last_activity": None,
            "usage": {"total_tokens": 0, "cost_usd": 0.0},
            "busy": False,
            "recent_messages": [],
            "recent_tasks": [
                {
                    "id": 7,
                    "status": "running",
                    "goal": "inspect file",
                    "remaining": ["summarize findings"],
                    "tools": [
                        {
                            "name": "read_file",
                            "source": "notes.txt",
                            "ok": True,
                            "result": "content",
                        }
                    ],
                }
            ],
        }
    )

    assert "còn lại: summarize findings" in text
    assert "tool read_file [notes.txt] (OK): content" in text


@pytest.mark.parametrize("args", ["", "CONFIRM", "confirm now", "/forget confirm extra"])
def test_forget_without_exact_confirm_only_warns(args: str) -> None:
    message = _FakeMessage()
    command = CommandObject(command="forget", args=args or None)
    _call(handlers.cmd_forget(message, 6201, command))
    assert len(message.answers) == 1
    assert "Gõ /forget confirm để xác nhận." in message.answers[0]


def test_forget_confirm_on_empty_memory_reports_empty() -> None:
    message = _FakeMessage()
    _call(
        handlers.cmd_forget(message, 6201, CommandObject(command="forget", args="confirm"))
    )
    assert message.answers == ["Bộ nhớ phiên đã trống; lịch sử usage vẫn được giữ."]


def test_forget_confirm_refuses_while_worker_lease_is_live() -> None:
    lease = try_reserve_turn(6201)
    assert lease is not None
    try:
        message = _FakeMessage()
        _call(
            handlers.cmd_forget(
                message, 6201, CommandObject(command="forget", args="confirm")
            )
        )
        assert message.answers == [
            "Lượt xử lý vẫn đang chạy. Hãy /cancel, chờ worker dừng rồi thử lại."
        ]
    finally:
        release_turn(lease)


def test_forget_confirm_deletes_owned_memory_and_discloses_legacy_rows() -> None:
    with session_scope() as session:
        repo.get_or_create_user(session, 6202, "legacy")
        session.add(ToolCall(tool_name="read_file"))  # task_id NULL: không owner

    message = _FakeMessage()
    _call(
        handlers.cmd_forget(
            message, 6202, CommandObject(command="forget", args="confirm")
        )
    )

    assert len(message.answers) == 1
    assert "Đã xóa bộ nhớ phiên có liên kết của bạn" in message.answers[0]
    assert "không có owner" in message.answers[0]
    with session_scope() as session:
        assert session.scalar(select(ToolCall.tool_name)) == "read_file"
