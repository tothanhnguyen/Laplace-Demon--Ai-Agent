import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from laplace import repo
from laplace.bot import handlers, runner
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import LLMCall, Reminder, Task, ToolApproval
from laplace.services import approvals, chat, memory
from laplace.services.reminders import ReminderScheduler


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    memory.start_chat_admission()
    monkeypatch.setattr(handlers, "UPLOAD_DIR", tmp_path)
    yield
    chat.interrupt_paused_turns()
    memory.start_chat_admission()
    reset_engine_for_tests(None)


class _MessagePipeline:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def middleware(self, _middleware) -> None:
        self.events.append("middleware")


class _Dispatcher:
    def __init__(
        self, events: list[str], error: Exception | None = None, on_polling=None
    ) -> None:
        self.events = events
        self.error = error
        self.message = _MessagePipeline(events)
        self.callback_query = _MessagePipeline(events)
        self.on_polling = on_polling

    def include_router(self, _router) -> None:
        self.events.append("router")

    async def start_polling(self, _bot, *, close_bot_session=True) -> None:
        try:
            self.events.append("polling")
            if self.on_polling is not None:
                self.on_polling()
            if self.error is not None:
                raise self.error
        finally:
            if close_bot_session:
                await _bot.session.close()


class _BotSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        assert not memory.accepts_chat_admission()
        assert not handlers._worker_tasks
        assert not handlers._admitted_tasks
        self.events.append("bot-close")


class _Bot:
    def __init__(self, events: list[str], send_error: Exception | None = None) -> None:
        self.session = _BotSession(events)
        self.events = events
        self.removed = []
        self.sent = []
        self.send_error = send_error

    async def edit_message_reply_markup(self, **kwargs):
        self.removed.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self.events.append("notice")
        if self.send_error is not None:
            raise self.send_error


class _Scheduler:
    def __init__(
        self,
        _bot,
        _settings,
        events: list[str],
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error

    def start(self) -> None:
        self.events.append("scheduler-start")

    def stop_jobs(self) -> None:
        self.events.append("scheduler-pause")

    async def shutdown(self) -> None:
        self.events.append("scheduler-stop")
        if self.error is not None:
            raise self.error


_LAST_BOT: list = []


def _install(
    monkeypatch,
    *,
    polling_error: Exception | None = None,
    scheduler_error: Exception | None = None,
    on_polling=None,
    send_error: Exception | None = None,
) -> list[str]:
    events: list[str] = []
    bot = _Bot(events, send_error)
    _LAST_BOT.append(bot)
    dispatcher = _Dispatcher(events, polling_error, on_polling)
    monkeypatch.setattr(
        runner,
        "Settings",
        lambda: Settings(_env_file=None, telegram_bot_token="token"),
    )
    monkeypatch.setattr(runner, "init_db", lambda: events.append("init-db"))
    monkeypatch.setattr(runner, "Bot", lambda token: bot)
    monkeypatch.setattr(runner, "Dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        runner,
        "ReminderScheduler",
        lambda bot, settings, maintenance=None: _Scheduler(
            bot, settings, events, scheduler_error
        ),
    )
    interrupt = chat.interrupt_paused_turns

    def interrupt_recorded():
        events.append("interrupt")
        return interrupt()

    monkeypatch.setattr(chat, "interrupt_paused_turns", interrupt_recorded)
    return events


def test_runner_stops_scheduler_before_bot_close(monkeypatch) -> None:
    events = _install(monkeypatch)
    asyncio.run(runner.run_bot())
    shutdown_interrupt = len(events) - 1 - events[::-1].index("interrupt")
    assert (
        events.index("scheduler-start")
        < events.index("polling")
        < events.index("scheduler-pause")
        < shutdown_interrupt
        < events.index("scheduler-stop")
        < events.index("bot-close")
    )


def test_runner_uses_same_cleanup_order_when_polling_fails(monkeypatch) -> None:
    events = _install(monkeypatch, polling_error=RuntimeError("polling failed"))
    with pytest.raises(RuntimeError, match="polling failed"):
        asyncio.run(runner.run_bot())
    assert events[-2:] == ["scheduler-stop", "bot-close"]


def test_runner_closes_bot_when_scheduler_shutdown_fails(monkeypatch) -> None:
    events = _install(monkeypatch, scheduler_error=RuntimeError("scheduler failed"))
    with pytest.raises(RuntimeError, match="scheduler failed"):
        asyncio.run(runner.run_bot())
    assert events[-2:] == ["scheduler-stop", "bot-close"]


def test_missing_token_fails_before_database_or_bot_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "Settings",
        lambda: Settings(_env_file=None, telegram_bot_token=""),
    )
    touched = []
    monkeypatch.setattr(runner, "init_db", lambda: touched.append("db"))
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        asyncio.run(runner.run_bot())
    assert touched == []


def _create_pending(monkeypatch, root: Path, *, owner=6220):
    provider = MockLLM(
        [
            {
                "action": "tool",
                "tool": "create_reminder",
                "params": {"message": "durable pending proposal", "delay_seconds": 180},
            },
            {"action": "final", "final_answer": "done"},
        ]
    )
    monkeypatch.setattr(chat, "get_provider", lambda: provider)
    path = root / f"laplace-pending-{owner}.txt"
    path.write_text("checkpoint material", encoding="utf-8")
    bundle = memory.AttachmentBundle({"file": path}, managed_root=root)
    lease = memory.admit_chat_turn(owner)
    assert lease is not None
    reply = chat._run_reserved_message(
        lease,
        owner,
        "owner",
        "/remind durable pending proposal",
        transport="telegram",
        is_private_chat=True,
        attachments=dict(bundle.paths),
        attachment_bundle=bundle,
        chat_id=owner,
    )
    assert reply.pause is not None
    record = approvals.get_approval(reply.pause.token)
    assert record is not None
    assert approvals.bind_preview(
        record.token, telegram_user_id=owner, chat_id=owner, message_id=500
    )
    return approvals.get_approval(record.token), path, provider


def test_startup_interruption_conflict_aborts_before_admission_and_keeps_checkpoint(
    tmp_path, monkeypatch
):
    record, retained, provider = _create_pending(monkeypatch, tmp_path)
    lease = memory.try_reserve_turn(6220, kind="chat")
    assert lease is not None
    try:
        assert approvals.claim_approval(
            record.token, telegram_user_id=6220, chat_id=6220, message_id=500
        ) is not None
    finally:
        memory.release_turn(lease)
    with session_scope() as session:
        session.get(Task, record.task_id).status = "failed"
    events = _install(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="Approval interruption failed"):
            asyncio.run(runner.run_bot())
        assert "polling" not in events
        assert memory.admit_chat_turn(6221) is None
        assert approvals.get_approval(record.token).status == "approved"
        assert chat.has_checkpoint(record.token, 6220) and retained.exists()
        with session_scope() as session:
            assert session.get(Task, record.task_id).status == "failed"
            assert session.scalar(select(Reminder.id)) is None
        assert len(provider.calls) == 1
    finally:
        # Repair only the injected test corruption; production never auto-heals it.
        with session_scope() as session:
            session.get(Task, record.task_id).status = "running"
    asyncio.run(runner.run_bot())
    assert approvals.get_approval(record.token).status == "interrupted"
    assert not chat.has_checkpoint(record.token, 6220) and not retained.exists()


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_startup_invalidates_proposals_before_polling_and_preserves_external_files(
    tmp_path, monkeypatch, status
):
    record, retained, provider = _create_pending(monkeypatch, tmp_path)
    if status == "approved":
        assert approvals.claim_approval(
            record.token, telegram_user_id=6220, chat_id=6220, message_id=500
        ) is not None
    orphan = tmp_path / "laplace-orphan.txt"
    orphan.write_text("old managed upload", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("caller owned", encoding="utf-8")
    symlink = tmp_path / "laplace-link.txt"
    symlink.symlink_to(external)
    directory = tmp_path / "laplace-directory"
    directory.mkdir()
    nested = directory / "laplace-nested.txt"
    nested.write_text("not a direct managed file", encoding="utf-8")

    def before_polling():
        assert approvals.get_approval(record.token).status == "interrupted"
        assert not chat.has_checkpoint(record.token, 6220)
        assert memory.paused_token(6220) is None
        assert not retained.exists() and not orphan.exists()
        assert external.read_text(encoding="utf-8") == "caller owned"
        assert symlink.is_symlink()
        assert nested.exists()
        with session_scope() as session:
            assert session.scalar(select(Task.status)) == "interrupted"
            assert session.scalar(select(Reminder.id)) is None

    _install(monkeypatch, on_polling=before_polling)
    asyncio.run(runner.run_bot())
    assert len(provider.calls) == 1
    assert not memory.accepts_chat_admission()


def test_shutdown_interrupts_pending_checkpoint_created_during_polling(tmp_path, monkeypatch):
    created = []

    def polling():
        created.append(_create_pending(monkeypatch, tmp_path))
        assert created[0][1].exists()

    events = _install(monkeypatch, on_polling=polling)
    asyncio.run(runner.run_bot())
    record, path, provider = created[0]
    assert approvals.get_approval(record.token).status == "interrupted"
    assert not path.exists()
    assert not chat.has_checkpoint(record.token, 6220)
    assert len(provider.calls) == 1
    assert events.index("scheduler-pause") < events.index("scheduler-stop")
    assert not memory.accepts_chat_admission()


def test_startup_retains_executed_receipt_without_replaying_effect(tmp_path, monkeypatch):
    record, path, provider = _create_pending(monkeypatch, tmp_path)
    lease = memory.try_reserve_turn(6220, kind="chat")
    assert lease is not None
    assert approvals.claim_approval(
        record.token, telegram_user_id=6220, chat_id=6220, message_id=500
    ) is not None
    reply = chat.resume_approved(lease, record.token)
    assert reply.pause is None
    receipt = approvals.get_approval(record.token).result_json
    assert receipt is not None
    with session_scope() as session:
        task = session.get(Task, record.task_id)
        task.status = "running"  # Crash after T3/T4, before the final task close.

    def before_polling():
        current = approvals.get_approval(record.token)
        assert current.status == "executed" and current.result_json == receipt
        with session_scope() as session:
            assert session.get(Task, record.task_id).status == "interrupted"
            assert len(list(session.scalars(select(Reminder)))) == 1

    _install(monkeypatch, on_polling=before_polling)
    asyncio.run(runner.run_bot())
    assert len(provider.calls) == 2
    assert not path.exists()


def test_scheduler_expiry_callback_cleans_real_checkpoint_and_stops_scanning(
    tmp_path, monkeypatch
):
    class Bot:
        def __init__(self):
            self.removed = []

        async def edit_message_reply_markup(self, **kwargs):
            self.removed.append(kwargs)

        async def send_message(self, **_kwargs):
            raise AssertionError("No pending reminder should be dispatched")

    bot = Bot()
    settings = Settings()

    async def scenario():
        record, path, _provider = _create_pending(monkeypatch, tmp_path)
        with session_scope() as session:
            session.get(ToolApproval, record.id).expires_at = approvals.epoch_now()
        scheduler = ReminderScheduler(
            bot, settings, maintenance=lambda: runner.expire_approval_previews(bot)
        )
        scheduler.start()
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while approvals.get_approval(record.token).status == "pending":
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
            assert approvals.get_approval(record.token).status == "expired"
            assert not path.exists()
            assert not chat.has_checkpoint(record.token, 6220)
            assert bot.removed == [
                {"chat_id": 6220, "message_id": 500, "reply_markup": None}
            ]
            scheduler.stop_jobs()
            second, second_path, _provider = _create_pending(monkeypatch, tmp_path, owner=6221)
            with session_scope() as session:
                session.get(ToolApproval, second.id).expires_at = approvals.epoch_now()
            await asyncio.sleep(1.1)
            assert approvals.get_approval(second.token).status == "pending"
            assert second_path.exists()
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_during_drain", [False, True])
def test_shutdown_cancels_cooperatively_and_drains_thread_before_closing_bot(
    tmp_path, monkeypatch, cancel_during_drain
):
    started = threading.Event()
    finish = threading.Event()
    events = []
    worker_handlers = []

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
                "params": {"message": "cancelled during shutdown", "delay_seconds": 180},
            }
        ]
    )
    monkeypatch.setattr(chat, "get_provider", lambda: provider)
    bot = _Bot(events)
    path = tmp_path / "laplace-running.txt"
    bundle = memory.AttachmentBundle({"file": path}, managed_root=tmp_path)

    class Status:
        async def edit_text(self, _text):
            assert "bot-close" not in events

    class Message:
        chat = SimpleNamespace(id=6220, type="private")

        async def answer(self, _text, **_kwargs):
            assert "bot-close" not in events
            return Status()

    message = Message()
    message.bot = bot

    async def send_action(_chat_id, _action):
        assert "bot-close" not in events

    bot.send_chat_action = send_action

    class Dispatcher(_Dispatcher):
        async def start_polling(self, _bot, *, close_bot_session=True):
            path.write_text("worker owns this", encoding="utf-8")
            worker_handlers.append(
                asyncio.create_task(
                    handlers._run_telegram_turn(
                        message,
                        6220,
                        "owner",
                        "/remind cancel on shutdown",
                        attachments=dict(bundle.paths),
                        attachment_bundle=bundle,
                    )
                )
            )
            assert await asyncio.to_thread(started.wait, 5)
            events.append("polling")
            if close_bot_session:
                await _bot.session.close()

    monkeypatch.setattr(
        runner,
        "Settings",
        lambda: Settings(_env_file=None, telegram_bot_token="token"),
    )
    monkeypatch.setattr(runner, "Bot", lambda token: bot)
    monkeypatch.setattr(runner, "Dispatcher", lambda: Dispatcher(events))
    monkeypatch.setattr(
        runner,
        "ReminderScheduler",
        lambda bot, settings, maintenance=None: _Scheduler(bot, settings, events),
    )

    async def scenario():
        running = asyncio.create_task(runner.run_bot())
        deadline = asyncio.get_running_loop().time() + 3
        while memory.accepts_chat_admission():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        assert memory.cancel_requested(6220)
        assert memory.user_busy(6220)
        assert path.exists()
        assert "bot-close" not in events
        assert memory.admit_chat_turn(6221) is None
        if cancel_during_drain:
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
            assert memory.user_busy(6220) and path.exists()
            assert "bot-close" not in events
        finish.set()
        if cancel_during_drain:
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            await running
        await asyncio.gather(*worker_handlers)
        assert events[-2:] == ["scheduler-stop", "bot-close"]
        assert not memory.user_busy(6220)
        assert not path.exists()
        with session_scope() as session:
            assert session.scalar(select(Reminder.id)) is None
            assert session.scalar(select(ToolApproval.id)) is None

    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_shutdown_drains_admitted_download_before_closing_transport(tmp_path, monkeypatch):
    downloading = asyncio.Event()
    finish_download = asyncio.Event()
    events = []
    downloads = []
    document_handlers = []
    bot = _Bot(events)

    async def download(_document, destination):
        downloads.append(Path(destination))
        Path(destination).write_text("downloaded material", encoding="utf-8")
        downloading.set()
        await finish_download.wait()
        assert "bot-close" not in events

    bot.download = download

    class Message:
        chat = SimpleNamespace(id=6220, type="private")
        document = SimpleNamespace(
            file_size=1, file_name="shutdown.txt", file_unique_id="shutdown"
        )
        caption = None

    message = Message()
    message.bot = bot

    class Dispatcher(_Dispatcher):
        async def start_polling(self, _bot, *, close_bot_session=True):
            document_handlers.append(
                asyncio.create_task(handlers.handle_document(message, 6220, "owner"))
            )
            await asyncio.wait_for(downloading.wait(), 5)
            events.append("polling")
            if close_bot_session:
                await _bot.session.close()

    monkeypatch.setattr(
        runner,
        "Settings",
        lambda: Settings(_env_file=None, telegram_bot_token="token"),
    )
    monkeypatch.setattr(runner, "Bot", lambda token: bot)
    monkeypatch.setattr(runner, "Dispatcher", lambda: Dispatcher(events))
    monkeypatch.setattr(
        runner,
        "ReminderScheduler",
        lambda bot, settings, maintenance=None: _Scheduler(bot, settings, events),
    )

    async def scenario():
        running = asyncio.create_task(runner.run_bot())
        await asyncio.wait_for(downloading.wait(), 5)
        deadline = asyncio.get_running_loop().time() + 3
        while memory.accepts_chat_admission():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0)
        assert memory.cancel_requested(6220) and memory.user_busy(6220)
        assert downloads[0].exists()
        assert "bot-close" not in events
        finish_download.set()
        await running
        await asyncio.gather(*document_handlers)
        assert not downloads[0].exists()
        assert not memory.user_busy(6220)
        assert events[-2:] == ["scheduler-stop", "bot-close"]
        with session_scope() as session:
            assert session.scalar(select(Task.id)) is None
            assert session.scalar(select(ToolApproval.id)) is None

    asyncio.run(scenario())


def test_startup_retains_reserved_charge_as_unknown_before_polling(monkeypatch):
    with session_scope() as session:
        user = repo.get_or_create_user(session, 6320, "crashed-worker")
        call = repo.record_llm_call(
            session, task_id=None, user_id=user.id, purpose="agent",
            provider="fixture", model="priced-fixture", prompt_tokens=0,
            completion_tokens=0, cost_usd=0.0, latency_ms=0,
            call_group_id="interrupted-group", attempt_index=0,
            accounting_status="reserved", budget_charge_usd=0.04,
        )
        attempt_id = call.id

    def before_polling():
        with session_scope() as session:
            call = session.get(LLMCall, attempt_id)
            assert call.accounting_status == "unknown"
            assert call.budget_charge_usd == 0.04
            assert call.cost_usd == 0
            assert json.loads(call.accounting_detail)["outcome"] == "interrupted"

    _install(monkeypatch, on_polling=before_polling)
    asyncio.run(runner.run_bot())


def test_failed_startup_attempt_recovery_keeps_admission_closed(monkeypatch):
    events = _install(monkeypatch)

    def fail_recovery(_session):
        raise RuntimeError("attempt recovery failed")

    monkeypatch.setattr(repo, "interrupt_reserved_llm_calls", fail_recovery)
    with pytest.raises(RuntimeError, match="attempt recovery failed"):
        asyncio.run(runner.run_bot())
    assert not memory.accepts_chat_admission()
    assert "polling" not in events
    assert "scheduler-start" not in events


def _active_task(telegram_user_id: int) -> int:
    with session_scope() as session:
        user = repo.get_or_create_user(session, telegram_user_id, None)
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(session, conversation, "user", "web task")
        task = repo.create_task(
            session, user_id=user.id, conversation_id=conversation.id,
            request_message_id=request.id, goal="web task",
        )
        return task.id


@pytest.mark.parametrize("send_error", [None, RuntimeError("blocked by user")])
def test_runner_sends_interrupted_notices_before_polling_and_tolerates_send_failures(
    monkeypatch, send_error
) -> None:
    task_id = _active_task(6301)
    _active_task(0)  # CLI identity: marked, never messaged
    events = _install(monkeypatch, send_error=send_error)
    bot = _LAST_BOT[-1]

    asyncio.run(runner.run_bot())

    assert events.index("notice") < events.index("scheduler-start") < events.index("polling")
    assert [message["chat_id"] for message in bot.sent] == [6301]
    assert bot.sent[0]["parse_mode"] is None
    assert bot.sent[0]["text"].startswith("Kết quả\nLượt xử lý trước bị gián đoạn")
    with session_scope() as session:
        assert session.get(Task, task_id).status == "interrupted"
        assert [task.status for task in session.scalars(select(Task))] == ["interrupted"] * 2
