"""M-12: dispatch và privacy của lệnh /memory, /forget qua handler thật."""

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select

from laplace import repo
from laplace.bot import handlers
from laplace.bot.middleware import IdentityRateLimitMiddleware
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import Reminder, Task, ToolApproval, ToolCall
from laplace.services import approvals, chat, memory
from laplace.services.memory import AttachmentBundle, release_turn, try_reserve_turn


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
    memory.start_chat_admission()
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    yield
    with session_scope() as session:
        tokens = list(session.scalars(select(ToolApproval.token)))
    for token in tokens:
        chat.discard_checkpoint(token)
    memory.start_chat_admission()
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
            "usage": {
                "total_tokens": 0, "cost_usd": 0.0, "llm_calls": 0,
                "physical_attempts": 0, "known_cost_usd": 0.0,
                "budget_charge_usd": 0.0, "unknown_attempts": 0,
            },
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
    paths = []

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

    async def fake_turn(
        _message, telegram_user_id, username, text, *, attachments, lease, attachment_bundle
    ) -> None:
        attachment_id, path = next(iter(attachments.items()))
        paths.append(path)
        captured.update(
            attachment_id=attachment_id,
            path=path,
            content=path.read_text(encoding="utf-8"),
            call=(telegram_user_id, username, text),
        )
        assert memory.user_busy(telegram_user_id)
        attachment_bundle.cleanup_worker()
        release_turn(lease)

    monkeypatch.setattr(handlers, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(handlers, "_run_telegram_turn", fake_turn)
    _call(handlers.handle_document(message, 6203, "user"))
    _call(handlers.handle_document(message, 6203, "user"))

    assert captured["attachment_id"] == "telegram-_opaque_id"
    assert captured["content"] == "document body"
    assert captured["call"] == (6203, "user", "summarize")
    assert len(set(paths)) == 2
    assert all(path.name.startswith("laplace-") and not path.exists() for path in paths)


@pytest.mark.parametrize("args", ["", "CONFIRM", "confirm now", "/forget confirm extra"])
def test_forget_without_exact_confirm_only_warns(args: str) -> None:
    message = _FakeMessage()
    command = CommandObject(command="forget", args=args or None)
    with session_scope() as session:
        user = repo.get_or_create_user(session, 6201, None)
        repo.get_or_create_conversation(session, user)
    _call(handlers.cmd_forget(message, 6201, command))
    assert len(message.answers) == 1
    with session_scope() as session:
        user = repo.find_user(session, 6201)
        assert user is not None
        assert repo.session_stats(session, user)["conversations"] == 1


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
    with session_scope() as session:
        assert session.scalar(select(ToolCall.tool_name)) == "read_file"
    with session_scope() as session:
        assert repo.find_user(session, 6202) is not None


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


class _TransportMessage(Message):
    """Real aiogram message identity, with Telegram I/O confined to a fake bot."""

    async def answer(self, text: str, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)

    async def edit_text(self, text: str, **_kwargs):
        self.bot.edits[self.message_id] = text
        return self

    async def edit_reply_markup(self, reply_markup=None, **_kwargs):
        return await self.bot.edit_message_reply_markup(
            chat_id=self.chat.id, message_id=self.message_id, reply_markup=reply_markup
        )


class _TransportBot:
    def __init__(self, *, fail_preview=False, fail_buttons=False) -> None:
        self.messages = {}
        self.send_options = {}
        self.markups = {}
        self.edits = {}
        self.downloads = []
        self.fail_preview = fail_preview
        self.fail_buttons = fail_buttons
        self._next_id = 100

    async def send_chat_action(self, _chat_id, _action):
        return None

    async def download(self, _document, destination):
        self.downloads.append(destination)
        Path(destination).write_text("owned upload", encoding="utf-8")

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_preview and "reply_markup" in kwargs:
            raise RuntimeError("preview send failed")
        self._next_id += 1
        sent = _transport_message(self, chat_id=chat_id, message_id=self._next_id, text=text)
        self.messages[sent.message_id] = sent
        self.send_options[sent.message_id] = kwargs
        self.markups[sent.message_id] = kwargs.get("reply_markup")
        return sent

    async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
        if reply_markup is not None:
            token = reply_markup.inline_keyboard[0][0].callback_data.split(":")[2]
            record = approvals.get_approval(token)
            assert record is not None
            assert (record.chat_id, record.preview_message_id) == (chat_id, message_id)
            assert record.status == "pending"
            if self.fail_buttons:
                raise RuntimeError("button publication failed")
        self.markups[message_id] = reply_markup
        return self.messages.get(message_id)


def _transport_message(
    bot, *, chat_id=6210, message_id=1, text="/remind", chat_type="private"
):
    return _TransportMessage(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=chat_id, is_bot=False, first_name="owner"),
        text=text,
    ).as_(bot)


class _Callback:
    def __init__(self, message, token, *, actor=6210, decision="a"):
        self.message = message
        self.from_user = SimpleNamespace(id=actor)
        self.data = f"ap:{decision}:{token}"
        self.inline_message_id = None
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


def _install_approval_provider(monkeypatch, *, provider=None):
    if provider is None:
        provider = MockLLM(
            script=[
                {
                    "action": "tool",
                    "tool": "create_reminder",
                    "params": {"message": "Đầy đủ\n🚀 private payload", "delay_seconds": 180},
                },
                {"action": "final", "final_answer": "Đã tạo reminder."},
            ]
        )
    monkeypatch.setattr(chat, "get_provider", lambda: provider)
    return provider


def _pending_record():
    with session_scope() as session:
        token = session.scalar(select(ToolApproval.token))
    assert token is not None
    record = approvals.get_approval(token)
    assert record is not None
    return record


async def _pause(bot, *, bundle=None):
    await handlers._run_telegram_turn(
        _transport_message(bot),
        6210,
        "owner",
        "/remind Đầy đủ sau 3 phút",
        attachments=dict(bundle.paths) if bundle else None,
        attachment_bundle=bundle,
    )
    return _pending_record()


def test_private_preview_binds_before_buttons_and_approve_resumes_same_task(monkeypatch):
    class CommitBeforeLLM(MockLLM):
        def complete(self, messages, *, options, json_schema=None):
            if self.calls:
                with session_scope() as session:
                    assert len(list(session.scalars(select(Reminder)))) == 1
                    assert session.scalar(select(ToolApproval.status)) == "executed"
            return super().complete(messages, options=options, json_schema=json_schema)

    provider = _install_approval_provider(
        monkeypatch,
        provider=CommitBeforeLLM(
            [
                {
                    "action": "tool",
                    "tool": "create_reminder",
                    "params": {"message": "Đầy đủ\n🚀 private payload", "delay_seconds": 180},
                },
                {"action": "final", "final_answer": "Đã tạo reminder."},
            ]
        ),
    )
    bot = _TransportBot()

    async def scenario():
        record = await _pause(bot)
        preview = bot.messages[record.preview_message_id]
        assert "Đầy đủ" in preview.text and "🚀 private payload" in preview.text
        assert bot.send_options[preview.message_id] == {"parse_mode": None, "reply_markup": None}
        buttons = bot.markups[preview.message_id].inline_keyboard[0]
        assert [button.callback_data for button in buttons] == [
            f"ap:a:{record.token}",
            f"ap:r:{record.token}",
        ]
        assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
        with session_scope() as session:
            assert session.scalar(select(Task.status)) == "awaiting_approval"
            assert session.scalar(select(Reminder.id)) is None
        callback = _Callback(preview, record.token)
        await handlers.handle_approval_callback(callback)
        assert len(callback.answers) == 1
        replay = _Callback(preview, record.token)
        await handlers.handle_approval_callback(replay)
        assert len(replay.answers) == 1
        with session_scope() as session:
            tasks = list(session.scalars(select(Task)))
            calls = list(session.scalars(select(ToolCall)))
            assert len(tasks) == 1 and tasks[0].id == record.task_id
            assert len(calls) == 1 and calls[0].approval_id == record.id
            assert calls[0].task_id == record.task_id
            assert len(list(session.scalars(select(Reminder)))) == 1
            assert session.scalar(select(ToolApproval.status)) == "executed"
        assert len(provider.calls) == 2
        assert not handlers._worker_tasks

    _call(scenario())


@pytest.mark.parametrize(
    "denial", ["owner", "chat", "message", "group", "inline", "missing", "expiry"]
)
def test_callback_denials_always_answer_without_consuming_pending(monkeypatch, denial):
    provider = _install_approval_provider(monkeypatch)
    bot = _TransportBot()

    async def scenario():
        record = await _pause(bot)
        message = bot.messages[record.preview_message_id]
        callback = _Callback(message, record.token)
        if denial == "owner":
            callback.from_user.id = 6211
        elif denial == "chat":
            callback.message = _transport_message(
                bot, chat_id=6211, message_id=record.preview_message_id
            )
        elif denial == "message":
            callback.message = _transport_message(bot, message_id=record.preview_message_id + 1)
        elif denial == "group":
            callback.message = _transport_message(
                bot, message_id=record.preview_message_id, chat_type="supergroup"
            )
        elif denial == "inline":
            callback.inline_message_id = "inline-origin"
        elif denial == "missing":
            callback.message = None
        else:
            with session_scope() as session:
                row = session.get(ToolApproval, record.id)
                row.expires_at = approvals.epoch_now()
        await handlers.handle_approval_callback(callback)
        assert len(callback.answers) == 1
        assert approvals.get_approval(record.token).status == "pending"
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None
        assert len(provider.calls) == 1
        assert not memory.user_busy(6210)

    _call(scenario())


def test_busy_then_double_click_claims_only_one_worker(monkeypatch):
    provider = _install_approval_provider(monkeypatch)
    bot = _TransportBot()

    async def scenario():
        record = await _pause(bot)
        preview = bot.messages[record.preview_message_id]
        lease = try_reserve_turn(6210, kind="reminder")
        assert lease is not None
        try:
            busy = _Callback(preview, record.token)
            await handlers.handle_approval_callback(busy)
            assert len(busy.answers) == 1
            assert approvals.get_approval(record.token).status == "pending"
        finally:
            release_turn(lease)
        first = _Callback(preview, record.token)
        second = _Callback(preview, record.token)
        await asyncio.gather(
            handlers.handle_approval_callback(first), handlers.handle_approval_callback(second)
        )
        assert len(first.answers) == len(second.answers) == 1
        assert len(provider.calls) == 2
        with session_scope() as session:
            assert len(list(session.scalars(select(Reminder)))) == 1
        assert not memory.user_busy(6210)

    _call(scenario())


@pytest.mark.parametrize("failure", ["send", "bind", "edit"])
def test_publication_failure_invalidates_and_cleans_owned_upload(tmp_path, monkeypatch, failure):
    _install_approval_provider(monkeypatch)
    bot = _TransportBot(fail_preview=failure == "send", fail_buttons=failure == "edit")
    if failure == "bind":
        monkeypatch.setattr(approvals, "bind_preview", lambda *_args, **_kwargs: False)
    path = tmp_path / "laplace-owned.txt"
    path.write_text("retained material", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        record = await _pause(bot, bundle=bundle)
        assert record.status == "invalidated"
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        assert memory.paused_token(6210) is None
        with session_scope() as session:
            assert session.scalar(select(Task.status)) == "failed"
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


def test_paused_admission_prevents_download_and_preserves_uploaded_generation(
    tmp_path, monkeypatch
):
    provider = _install_approval_provider(monkeypatch)
    bot = _TransportBot()
    path = tmp_path / "laplace-retained.txt"
    path.write_text("original paused upload", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)
    monkeypatch.setattr(handlers, "UPLOAD_DIR", tmp_path)

    async def scenario():
        record = await _pause(bot, bundle=bundle)
        bundle.cleanup_worker()
        assert path.read_text(encoding="utf-8") == "original paused upload"
        document = _transport_message(bot).model_copy(
            update={
                "document": SimpleNamespace(
                    file_size=12, file_name="retained.txt", file_unique_id="file"
                ),
                "caption": "/remind another reminder",
            }
        )
        await handlers.handle_document(document, 6210, "owner")
        await handlers.handle_text(_transport_message(bot, text="new request"), 6210, "owner")
        assert bot.downloads == []
        assert path.exists()
        assert chat.has_checkpoint(record.token, 6210)
        assert len(provider.calls) == 1
        rejected = _Callback(bot.messages[record.preview_message_id], record.token, decision="r")
        await handlers.handle_approval_callback(rejected)
        assert len(rejected.answers) == 1
        assert approvals.get_approval(record.token).status == "rejected"
        assert not path.exists()
        assert bot.markups[record.preview_message_id] is None
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


@pytest.mark.parametrize("failure", ["task", "executor"])
def test_approval_worker_launch_failure_interrupts_claim_and_releases_lease(
    tmp_path, monkeypatch, failure
):
    _install_approval_provider(monkeypatch)
    bot = _TransportBot()
    path = tmp_path / "laplace-launch.txt"
    path.write_text("upload", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        record = await _pause(bot, bundle=bundle)

        def fail_launch(*_args, **_kwargs):
            raise RuntimeError("worker cannot launch")

        if failure == "task":
            monkeypatch.setattr(handlers, "_launch_worker", fail_launch)
        else:
            async def executor_unavailable(*_args, **_kwargs):
                raise RuntimeError("executor cannot launch")

            monkeypatch.setattr(handlers.asyncio, "to_thread", executor_unavailable)
        callback = _Callback(bot.messages[record.preview_message_id], record.token)
        await handlers.handle_approval_callback(callback)
        assert len(callback.answers) == 1
        assert approvals.get_approval(record.token).status == "interrupted"
        assert not memory.user_busy(6210)
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


def test_detached_worker_drains_real_thread_before_cleanup_and_invalidates_unpublished_pause(
    tmp_path, monkeypatch
):
    started = threading.Event()
    finish = threading.Event()

    class BlockingProvider(MockLLM):
        def complete(self, messages, *, options, json_schema=None):
            result = super().complete(messages, options=options, json_schema=json_schema)
            started.set()
            assert finish.wait(5)
            return result

    provider = BlockingProvider(
        [
            {
                "action": "tool",
                "tool": "create_reminder",
                "params": {"message": "retained until worker finish", "delay_seconds": 180},
            }
        ]
    )
    _install_approval_provider(monkeypatch, provider=provider)
    bot = _TransportBot()
    path = tmp_path / "laplace-detached.txt"
    path.write_text("upload still in use", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        handler = asyncio.create_task(_pause(bot, bundle=bundle))
        assert await asyncio.to_thread(started.wait, 5)
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert path.exists()
        assert memory.user_busy(6210)
        drain = asyncio.create_task(handlers.drain_chat_workers())
        await asyncio.sleep(0)
        assert not drain.done()
        finish.set()
        await drain
        assert not path.exists()
        assert not memory.user_busy(6210)
        record = _pending_record()
        assert record.status == "invalidated"
        assert record.preview_message_id is None
        assert not chat.has_checkpoint(record.token, 6210)

    try:
        _call(scenario())
    finally:
        finish.set()


@pytest.mark.parametrize("command", ["cancel", "forget"])
def test_account_commands_cleanup_pending_only_after_terminal_commit(
    tmp_path, monkeypatch, command
):
    _install_approval_provider(monkeypatch)
    bot = _TransportBot()
    path = tmp_path / "laplace-command.txt"
    path.write_text("paused", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        record = await _pause(bot, bundle=bundle)
        message = _transport_message(bot)
        if command == "cancel":
            await handlers.cmd_cancel(message, 6210)
            assert approvals.get_approval(record.token).status == "cancelled"
        else:
            await handlers.cmd_forget(
                message, 6210, CommandObject(command="forget", args="confirm")
            )
            assert approvals.get_approval(record.token) is None
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        assert memory.paused_token(6210) is None
        assert bot.markups[record.preview_message_id] is None
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


@pytest.mark.parametrize("failure", ["download", "cancel", "size"])
def test_download_failure_or_oversize_releases_admission_and_owned_file(
    tmp_path, monkeypatch, failure
):
    class Bot:
        async def download(self, _document, destination):
            assert memory.user_busy(6210)
            Path(destination).write_text("too much material", encoding="utf-8")
            if failure == "download":
                raise RuntimeError("download failed")
            if failure == "cancel":
                raise asyncio.CancelledError

    message = _FakeMessage()
    message.chat.id = 6210
    message.bot = Bot()
    message.caption = None
    message.document = SimpleNamespace(file_size=1, file_name="upload.txt", file_unique_id="same")
    monkeypatch.setattr(handlers, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        handlers,
        "Settings",
        lambda: Settings(_env_file=None, document_max_bytes=5),
    )
    if failure == "download":
        with pytest.raises(RuntimeError, match="download failed"):
            _call(handlers.handle_document(message, 6210, "owner"))
    elif failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _call(handlers.handle_document(message, 6210, "owner"))
    else:
        _call(handlers.handle_document(message, 6210, "owner"))
    assert not memory.user_busy(6210)
    assert list(tmp_path.iterdir()) == []
    with session_scope() as session:
        assert session.scalar(select(Task.id)) is None


def test_callback_middleware_preserves_actor_when_new_turn_quota_is_exhausted():
    class ExhaustedBucket:
        def allow(self, _user_id):
            raise AssertionError("Consent callback must not consume new-turn quota")

    callback = CallbackQuery(
        id="opaque",
        from_user=User(id=6210, is_bot=False, first_name="owner", username="actual"),
        chat_instance="instance",
        data="ap:r:opaque-token",
    )
    seen = []

    async def receive(_event, data):
        seen.append((data["telegram_user_id"], data["username"]))

    middleware = IdentityRateLimitMiddleware(ExhaustedBucket())
    _call(middleware(receive, callback, {"telegram_user_id": 999, "username": "wrong"}))
    assert seen == [(6210, "actual")]


def test_callback_database_error_answers_and_releases_unclaimed_lease(monkeypatch):
    _install_approval_provider(monkeypatch)
    bot = _TransportBot()

    async def scenario():
        record = await _pause(bot)

        def claim_failed(*_args, **_kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(approvals, "claim_approval", claim_failed)
        callback = _Callback(bot.messages[record.preview_message_id], record.token)
        await handlers.handle_approval_callback(callback)
        assert len(callback.answers) == 1
        assert not memory.user_busy(6210)
        assert approvals.get_approval(record.token).status == "pending"
        assert chat.has_checkpoint(record.token, 6210)
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


@pytest.mark.parametrize("stage", ["send", "bind", "edit"])
def test_cancelled_publication_invalidates_before_worker_cleanup(tmp_path, monkeypatch, stage):
    _install_approval_provider(monkeypatch)

    class Bot(_TransportBot):
        async def send_message(self, chat_id, text, **kwargs):
            if stage == "send" and "reply_markup" in kwargs:
                raise asyncio.CancelledError
            return await super().send_message(chat_id, text, **kwargs)

        async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
            if stage == "edit" and reply_markup is not None:
                raise asyncio.CancelledError
            return await super().edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            )

    if stage == "bind":
        def cancel_binding(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(approvals, "bind_preview", cancel_binding)
    path = tmp_path / "laplace-cancel-publication.txt"
    path.write_text("still owned", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await _pause(Bot(), bundle=bundle)
        await handlers.drain_chat_workers()
        record = _pending_record()
        assert record.status == "invalidated"
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        assert not memory.user_busy(6210)
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


def test_next_pause_delivers_committed_receipt_before_material_preview(monkeypatch):
    provider = _install_approval_provider(
        monkeypatch,
        provider=MockLLM(
            [
                {
                    "action": "tool",
                    "tool": "create_reminder",
                    "params": {"message": "first committed effect", "delay_seconds": 180},
                },
                {
                    "action": "tool",
                    "tool": "create_reminder",
                    "params": {"message": "second unapproved effect", "delay_seconds": 240},
                },
            ]
        ),
    )
    bot = _TransportBot()

    async def scenario():
        first = await _pause(bot)
        await handlers.handle_approval_callback(
            _Callback(bot.messages[first.preview_message_id], first.token)
        )
        with session_scope() as session:
            pending_token = session.scalar(
                select(ToolApproval.token).where(ToolApproval.status == "pending")
            )
            reminder = session.scalar(select(Reminder))
            assert reminder.message == "first committed effect"
            committed_id = reminder.id
        second = approvals.get_approval(pending_token)
        assert second is not None and second.task_id == first.task_id
        preceding = [
            message for message_id, message in bot.messages.items()
            if first.preview_message_id < message_id < second.preview_message_id
            and bot.send_options[message_id].get("parse_mode", "default") is None
        ]
        assert any(f"#{committed_id}" in message.text for message in preceding)
        await handlers.handle_approval_callback(
            _Callback(bot.messages[second.preview_message_id], second.token, decision="r")
        )
        assert approvals.get_approval(first.token).status == "executed"
        assert approvals.get_approval(second.token).status == "rejected"
        with session_scope() as session:
            assert list(session.scalars(select(Reminder.message))) == ["first committed effect"]
        assert len(provider.calls) == 2

    _call(scenario())


@pytest.mark.parametrize("command", ["cancel", "forget"])
def test_late_preview_edit_cannot_restore_keyboard_after_account_cleanup(
    tmp_path, monkeypatch, command
):
    _install_approval_provider(monkeypatch)
    editing = asyncio.Event()
    finish_edit = asyncio.Event()

    class Bot(_TransportBot):
        async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
            result = await super().edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            )
            if reply_markup is not None:
                editing.set()
                await finish_edit.wait()
                self.markups[message_id] = reply_markup
            return result

    bot = Bot()
    path = tmp_path / "laplace-late-edit.txt"
    path.write_text("awaiting preview", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        publishing = asyncio.create_task(
            handlers._run_telegram_turn(
                _transport_message(bot),
                6210,
                "owner",
                "/remind awaiting preview",
                attachments=dict(bundle.paths),
                attachment_bundle=bundle,
            )
        )
        await asyncio.wait_for(editing.wait(), 5)
        record = _pending_record()
        message = _transport_message(bot)
        if command == "cancel":
            await handlers.cmd_cancel(message, 6210)
        else:
            await handlers.cmd_forget(
                message, 6210, CommandObject(command="forget", args="confirm")
            )
        assert not path.exists()
        finish_edit.set()
        await publishing
        assert bot.markups[record.preview_message_id] is None
        assert not chat.has_checkpoint(record.token, 6210)
        assert not memory.user_busy(6210)
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


@pytest.mark.parametrize("stage", ["send", "edit"])
def test_handler_detaches_during_preview_io_without_leaving_approval_or_upload(
    tmp_path, monkeypatch, stage
):
    _install_approval_provider(monkeypatch)
    publishing = asyncio.Event()
    finish_publication = asyncio.Event()

    class Bot(_TransportBot):
        async def send_message(self, chat_id, text, **kwargs):
            result = await super().send_message(chat_id, text, **kwargs)
            if stage == "send" and "reply_markup" in kwargs:
                publishing.set()
                await finish_publication.wait()
            return result

        async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
            result = await super().edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            )
            if stage == "edit" and reply_markup is not None:
                publishing.set()
                await finish_publication.wait()
            return result

    bot = Bot()
    path = tmp_path / "laplace-detached-preview.txt"
    path.write_text("checkpoint upload", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        handler = asyncio.create_task(_pause(bot, bundle=bundle))
        await asyncio.wait_for(publishing.wait(), 5)
        record = _pending_record()
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert path.exists()
        finish_publication.set()
        await handlers.drain_chat_workers()
        assert approvals.get_approval(record.token).status == "invalidated"
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        assert not any(bot.markups.values())
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


def test_cancelled_worker_before_thread_entry_revokes_claim_and_releases_upload(
    tmp_path, monkeypatch
):
    provider = _install_approval_provider(monkeypatch)
    bot = _TransportBot()
    path = tmp_path / "laplace-unstarted.txt"
    path.write_text("checkpoint upload", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        record = await _pause(bot, bundle=bundle)
        lease = try_reserve_turn(6210, kind="chat")
        assert lease is not None
        assert approvals.claim_approval(
            record.token,
            telegram_user_id=6210,
            chat_id=6210,
            message_id=record.preview_message_id,
        ) is not None
        preview = bot.messages[record.preview_message_id]
        task = handlers._launch_worker(
            6210,
            preview,
            handlers._StatusEditor(preview, asyncio.get_running_loop()),
            lambda: chat.resume_approved(lease, record.token),
            None,
            asyncio.Event(),
            lease=lease,
            approval_token=record.token,
        )
        task.cancel()
        await handlers.drain_chat_workers()
        assert approvals.get_approval(record.token).status == "interrupted"
        assert not memory.user_busy(6210)
        assert not path.exists()
        assert not chat.has_checkpoint(record.token, 6210)
        assert len(provider.calls) == 1
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None

    _call(scenario())


def test_repeated_worker_cancellation_drains_thread_before_releasing_upload(
    tmp_path, monkeypatch
):
    started = threading.Event()
    finish = threading.Event()

    class BlockingProvider(MockLLM):
        def complete(self, messages, *, options, json_schema=None):
            result = super().complete(messages, options=options, json_schema=json_schema)
            started.set()
            assert finish.wait(5)
            return result

    _install_approval_provider(
        monkeypatch,
        provider=BlockingProvider(
            [
                {
                    "action": "tool",
                    "tool": "create_reminder",
                    "params": {"message": "cancel before approval", "delay_seconds": 180},
                }
            ]
        ),
    )
    bot = _TransportBot()
    path = tmp_path / "laplace-repeated-cancel.txt"
    path.write_text("worker material", encoding="utf-8")
    bundle = AttachmentBundle({"file": path}, managed_root=tmp_path)

    async def scenario():
        handler = asyncio.create_task(
            handlers._run_telegram_turn(
                _transport_message(bot),
                6210,
                "owner",
                "/remind cancel before approval",
                attachments=dict(bundle.paths),
                attachment_bundle=bundle,
            )
        )
        assert await asyncio.to_thread(started.wait, 5)
        worker = next(iter(handlers._worker_tasks))
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(0)
        assert not worker.done()
        assert path.exists() and memory.user_busy(6210)
        finish.set()
        await handler
        await handlers.drain_chat_workers()
        assert not path.exists() and not memory.user_busy(6210)
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None
            assert session.scalar(select(ToolApproval.id)) is None

    try:
        _call(scenario())
    finally:
        finish.set()


def test_status_and_memory_keep_unknown_reserve_owner_scoped_after_forget():
    with session_scope() as session:
        owner = repo.get_or_create_user(session, 6301, "owner")
        conversation = repo.get_or_create_conversation(session, owner)
        request = repo.add_message(session, conversation, "user", "private request")
        for index, status, known, charge in [
            (0, "unknown", 0.0, 0.05),
            (1, "settled", 0.002, 0.002),
        ]:
            repo.record_llm_call(
                session, task_id=None, user_id=owner.id, purpose="agent",
                provider="fixture", model="priced-fixture",
                prompt_tokens=3 if status == "settled" else 0,
                completion_tokens=2 if status == "settled" else 0,
                cost_usd=known, latency_ms=1, request_message_id=request.id,
                call_group_id="owner-retry-group", attempt_index=index,
                accounting_status=status, budget_charge_usd=charge,
            )
        other = repo.get_or_create_user(session, 6302, "other")
        repo.record_llm_call(
            session, task_id=None, user_id=other.id, purpose="agent",
            provider="legacy", model="historical", prompt_tokens=100,
            completion_tokens=10, cost_usd=0.42, latency_ms=1,
        )

    message = _FakeMessage()
    _call(handlers.cmd_status(message, 6301, "owner"))
    status_before = message.answers[-1]
    assert "logic / attempts: 1 / 2" in status_before
    assert "$0.002000" in status_before
    assert "$0.052000" in status_before
    assert "chưa biết usage: 1" in status_before
    assert "$0.420000" not in status_before

    _call(handlers.cmd_memory(message, 6301))
    assert "$0.002000" in message.answers[-1]
    assert "$0.052000" in message.answers[-1]
    assert "chưa biết usage: 1" in message.answers[-1]
    _call(handlers.cmd_forget(
        message, 6301, CommandObject(command="forget", args="confirm"),
    ))
    _call(handlers.cmd_status(message, 6301, "owner"))
    assert message.answers[-1] == status_before
