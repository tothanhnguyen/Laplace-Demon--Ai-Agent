"""M-12: dispatch và privacy của lệnh /memory, /forget qua handler thật."""

import asyncio
from pathlib import Path
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
                            "name": "web_search",
                            "sources": ["https://example.com/result"],
                            "ok": True,
                            "result": "content",
                        }
                    ],
                }
            ],
            "recent_reminders": [
                {
                    "id": 9,
                    "status": "pending",
                    "run_at": "2026-01-01T12:00:00",
                    "message": "call home",
                }
            ],
        }
    )

    assert "còn lại: summarize findings" in text
    assert "#9 [pending] 2026-01-01T12:00:00 (UTC): call home" in text


def test_document_processing_is_private_only() -> None:
    class Bot:
        async def download(self, _document, destination) -> None:
            raise AssertionError("group document must not be downloaded")

    message = _FakeMessage(chat_type="supergroup")
    message.bot = Bot()
    message.caption = None
    message.document = SimpleNamespace(
        file_size=10,
        file_name="sample.txt",
        file_unique_id="opaque",
    )
    _call(handlers.handle_document(message, 6203, "user"))
    assert message.answers == ["Hãy mở chat riêng với bot để xử lý tài liệu."]


def test_document_turn_uses_opaque_attachment_and_cleans_file(tmp_path, monkeypatch) -> None:
    captured = {}

    class Bot:
        async def download(self, _document, destination) -> None:
            Path(destination).write_text("document body", encoding="utf-8")

    message = _FakeMessage()
    message.chat.id = 6203
    message.bot = Bot()
    message.caption = "summarize"
    message.document = SimpleNamespace(
        file_size=13,
        file_name="../unsafe.txt",
        file_unique_id="../opaque/id",
    )

    async def fake_turn(_message, telegram_user_id, username, text, *, attachments) -> None:
        attachment_id, path = next(iter(attachments.items()))
        captured.update(
            attachment_id=attachment_id,
            path=path,
            content=path.read_text(encoding="utf-8"),
            call=(telegram_user_id, username, text),
        )
        path.unlink()

    monkeypatch.setattr(handlers, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(handlers, "_run_telegram_turn", fake_turn)
    _call(handlers.handle_document(message, 6203, "user"))

    assert captured["attachment_id"] == "telegram-_opaque_id"
    assert captured["content"] == "document body"
    assert captured["call"] == (6203, "user", "summarize")
    assert not captured["path"].exists()


@pytest.mark.parametrize("args", ["", "CONFIRM", "confirm now", "/forget confirm extra"])
def test_forget_without_exact_confirm_only_warns(args: str) -> None:
    message = _FakeMessage()
    command = CommandObject(command="forget", args=args or None)
    _call(handlers.cmd_forget(message, 6201, command))
    assert len(message.answers) == 1
    assert "Gõ /forget confirm để xác nhận." in message.answers[0]
    assert "tệp upload tạm đã được xóa sau lượt xử lý" in message.answers[0]


def test_forget_confirm_on_empty_memory_reports_empty() -> None:
    message = _FakeMessage()
    _call(handlers.cmd_forget(message, 6201, CommandObject(command="forget", args="confirm")))
    assert message.answers == ["Bộ nhớ phiên đã trống; lịch sử usage vẫn được giữ."]


def test_forget_confirm_refuses_while_worker_lease_is_live() -> None:
    lease = try_reserve_turn(6201)
    assert lease is not None
    try:
        message = _FakeMessage()
        _call(handlers.cmd_forget(message, 6201, CommandObject(command="forget", args="confirm")))
        assert message.answers == [
            "Đang có thao tác tài khoản (chat, tài liệu hoặc reminder). "
            "Chờ hoàn tất rồi dùng /forget confirm lại."
        ]
    finally:
        release_turn(lease)


def test_forget_confirm_deletes_owned_memory_and_discloses_legacy_rows() -> None:
    with session_scope() as session:
        repo.get_or_create_user(session, 6202, "legacy")
        session.add(ToolCall(tool_name="read_file"))  # task_id NULL: không owner

    message = _FakeMessage()
    _call(handlers.cmd_forget(message, 6202, CommandObject(command="forget", args="confirm")))

    assert len(message.answers) == 1
    assert "Đã xóa bộ nhớ phiên và reminder có liên kết của bạn" in message.answers[0]
    assert "không có owner" in message.answers[0]
    assert "Tệp upload tạm đã được xóa sau lượt xử lý" in message.answers[0]
    with session_scope() as session:
        assert session.scalar(select(ToolCall.tool_name)) == "read_file"


def test_reminder_directives_reach_shared_turn_while_unknown_commands_do_not(
    monkeypatch,
) -> None:
    captured: list[tuple[int, str | None, str]] = []

    async def fake_turn(message, telegram_user_id, username, text, **_kwargs) -> None:
        captured.append((telegram_user_id, username, text))

    monkeypatch.setattr(handlers, "_run_telegram_turn", fake_turn)
    reminder = _FakeMessage()
    reminder.text = "/remind gọi mẹ sau 10 phút"
    _call(handlers.handle_text(reminder, 6204, "directive"))
    assert captured == [(6204, "directive", "/remind gọi mẹ sau 10 phút")]
    assert reminder.answers == []

    unknown = _FakeMessage()
    unknown.text = "/unknown"
    _call(handlers.handle_text(unknown, 6204, "directive"))
    assert unknown.answers == ["Lệnh không được hỗ trợ. Gõ /help để xem hướng dẫn."]
