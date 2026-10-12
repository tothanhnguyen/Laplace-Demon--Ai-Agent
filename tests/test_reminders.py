import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import laplace.tools.reminders  # noqa: F401
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.models import Reminder, User
from laplace.repo import create_reminder, get_or_create_user, reminder_for_owner, session_stats
from laplace.services.memory import forget_memory, release_turn, try_reserve_turn
from laplace.services.reminders import (
    ReminderScheduler,
    dispatch_due,
    epoch_utc,
    recover_on_startup,
)
from laplace.tools.base import ToolContext, execute


@pytest.fixture(autouse=True)
def _database(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'reminders.db'}"
    reset_engine_for_tests(database_url)
    init_db()
    yield database_url
    reset_engine_for_tests()


def _user(telegram_id: int) -> int:
    with session_scope() as session:
        return get_or_create_user(session, telegram_id, None).id


def _context(
    user_id: int,
    telegram_id: int,
    *,
    private: bool = True,
    effects: frozenset[str] = frozenset({"create_reminder", "update_reminder", "cancel_reminder"}),
) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        telegram_user_id=telegram_id,
        transport="telegram",
        is_private_chat=private,
        conversation_id=1,
        request_message_id=1,
        task_id=1,
        step_index=1,
        now_utc=datetime(2026, 1, 1, 12, tzinfo=UTC),
        timezone="UTC",
        authorized_effects=effects,
    )


def _create(user_id: int, due_at: int, message: str = "due") -> int:
    with session_scope() as session:
        reminder = create_reminder(
            session,
            user_id=user_id,
            message=message,
            timezone="UTC",
            due_at_utc=due_at,
        )
        return reminder.id


class _Bot:
    def __init__(self, error: BaseException | None = None, message_id: int = 321) -> None:
        self.error = error
        self.message_id = message_id
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self.message_id)


def test_create_update_cancel_and_owner_isolation() -> None:
    owner = _user(101)
    other = _user(202)
    created = execute(
        "create_reminder",
        {"message": "call home", "delay_seconds": 60},
        _context(owner, 101),
    )
    reminder_id = created.data["id"]
    assert created.data == {
        "id": reminder_id,
        "message": "call home",
        "run_at": "2026-01-01T12:01:00Z",
        "status": "pending",
    }

    hidden = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(other, 202))
    assert hidden.error_code == "not_found"

    updated = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "call dad", "delay_seconds": 120},
        _context(owner, 101),
    )
    assert updated.data["run_at"] == "2026-01-01T12:02:00Z"
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 101))
    assert cancelled.data["status"] == "cancelled"
    immutable = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "again"},
        _context(owner, 101),
    )
    assert immutable.error_code == "invalid_input"


def test_reminder_mutation_requires_private_direct_request() -> None:
    owner = _user(303)
    forbidden_group = execute(
        "create_reminder",
        {"message": "x", "delay_seconds": 1},
        _context(owner, 303, private=False),
    )
    forbidden_injection = execute(
        "create_reminder",
        {"message": "injected", "delay_seconds": 60},
        _context(owner, 303, effects=frozenset()),
    )
    invalid_offset = execute(
        "create_reminder",
        {"message": "x", "run_at": "2026-01-01T13:00:00+02:00"},
        _context(owner, 303),
    )
    assert forbidden_group.error_code == forbidden_injection.error_code == "forbidden"
    assert invalid_offset.error_code == "invalid_input"


def test_failed_or_unknown_requires_future_time_to_rearm() -> None:
    owner = _user(304)
    reminder_id = _create(owner, epoch_utc(datetime(2026, 1, 2, tzinfo=UTC)))
    with session_scope() as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = "delivery_unknown"
        reminder.attempt_count = 3

    message_only = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "new"},
        _context(owner, 304),
    )
    rearmed = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "new", "delay_seconds": 120},
        _context(owner, 304),
    )
    assert message_only.error_code == "invalid_input"
    assert rearmed.data["status"] == "pending"
    with session_scope() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.attempt_count == 0
        assert reminder.last_error is None


def test_update_and_cancel_cannot_cross_delivery_claim() -> None:
    owner = _user(909)
    reminder_id = _create(owner, epoch_utc(datetime(2026, 1, 2, tzinfo=UTC)), "original")
    with session_scope() as session:
        session.get(Reminder, reminder_id).status = "delivering"

    updated = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "changed"},
        _context(owner, 909),
    )
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 909))
    assert updated.error_code == cancelled.error_code == "invalid_input"
    with session_scope() as session:
        stored = session.get(Reminder, reminder_id)
        assert (stored.status, stored.message) == ("delivering", "original")


def test_dispatch_leases_before_claim_and_records_provider_evidence() -> None:
    owner = _user(404)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    reminder_id = _create(owner, now_epoch - 1)
    settings = Settings(reminder_misfire_grace_seconds=60)

    busy = try_reserve_turn(404)
    assert busy is not None
    try:
        assert asyncio.run(dispatch_due(_Bot(), settings, now_utc=now)) == 0
        with session_scope() as session:
            assert session.get(Reminder, reminder_id).status == "pending"
    finally:
        release_turn(busy)

    retry_now = now + timedelta(seconds=1)
    bot = _Bot(message_id=987)
    assert asyncio.run(dispatch_due(bot, settings, now_utc=retry_now)) == 1
    assert asyncio.run(dispatch_due(bot, settings, now_utc=now)) == 0
    assert len(bot.sent) == 1
    assert f"Nhắc việc #{reminder_id}" in bot.sent[0][1]
    with session_scope() as session:
        stored = session.get(Reminder, reminder_id)
        assert stored.status == "sent"
        assert stored.attempt_count == 1
        assert stored.first_attempt_at_utc == stored.last_attempt_at_utc == now_epoch + 1
        assert stored.sent_at_utc == now_epoch + 1
        assert stored.telegram_message_id == 987
        assert stored.first_attempt_at_utc - stored.due_at_utc <= 2


def test_retryable_failures_honor_retry_after_and_backoff() -> None:
    class TelegramRetryAfter(Exception):
        retry_after = 7

    class TelegramServerError(Exception):
        pass

    owner = _user(505)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    reminder_id = _create(owner, now_epoch)
    settings = Settings(reminder_misfire_grace_seconds=60, reminder_max_attempts=3)

    assert asyncio.run(dispatch_due(_Bot(TelegramRetryAfter()), settings, now_utc=now)) == 0
    with session_scope() as session:
        stored = session.get(Reminder, reminder_id)
        assert (stored.status, stored.attempt_count) == ("pending", 1)
        assert stored.next_attempt_at_utc == now_epoch + 7

    server_id = _create(owner, now_epoch, "server failure")
    assert asyncio.run(dispatch_due(_Bot(TelegramServerError()), settings, now_utc=now)) == 0
    with session_scope() as session:
        stored = session.get(Reminder, server_id)
        assert (stored.status, stored.attempt_count) == ("pending", 1)
        assert stored.next_attempt_at_utc == now_epoch + 30

    class MissingMessageIdBot:
        async def send_message(self, chat_id: int, text: str):
            return SimpleNamespace()

    ambiguous_id = _create(owner, now_epoch, "ambiguous response")
    assert asyncio.run(dispatch_due(MissingMessageIdBot(), settings, now_utc=now)) == 0
    with session_scope() as session:
        stored = session.get(Reminder, ambiguous_id)
        assert (stored.status, stored.attempt_count) == ("pending", 1)
        assert stored.last_error == "AmbiguousDeliveryError"


def test_cancelled_inflight_send_requeues_and_releases_user_lease() -> None:
    owner = _user(504)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    reminder_id = _create(owner, epoch_utc(now))
    started = asyncio.Event()

    class BlockingBot:
        async def send_message(self, chat_id: int, text: str):
            started.set()
            await asyncio.Event().wait()

    async def scenario() -> None:
        task = asyncio.create_task(
            dispatch_due(
                BlockingBot(),
                Settings(reminder_misfire_grace_seconds=60),
                now_utc=now,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    lease = try_reserve_turn(504)
    assert lease is not None
    release_turn(lease)
    with session_scope() as session:
        stored = session.get(Reminder, reminder_id)
        assert (stored.status, stored.attempt_count) == ("pending", 1)


def test_busy_owners_do_not_starve_later_due_users() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    owners = [_user(5101), _user(5102), _user(5103)]
    reminder_ids = [
        _create(owner, now_epoch, f"owner {index}") for index, owner in enumerate(owners)
    ]
    leases = [try_reserve_turn(5101), try_reserve_turn(5102)]
    assert all(lease is not None for lease in leases)
    bot = _Bot()
    try:
        sent = asyncio.run(
            dispatch_due(
                bot,
                Settings(reminder_batch_size=2, reminder_scan_interval_seconds=1),
                now_utc=now,
            )
        )
    finally:
        for lease in leases:
            release_turn(lease)

    assert sent == 1
    assert [chat_id for chat_id, _text in bot.sent] == [5103]
    with session_scope() as session:
        first = session.get(Reminder, reminder_ids[0])
        second = session.get(Reminder, reminder_ids[1])
        third = session.get(Reminder, reminder_ids[2])
        assert (first.status, second.status, third.status) == ("pending", "pending", "sent")
        assert first.next_attempt_at_utc == second.next_attempt_at_utc == now_epoch + 1


def test_busy_candidate_scan_is_bounded_and_advances_fairly() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    owner = _user(5150)
    reminder_ids = [_create(owner, now_epoch, f"busy {index}") for index in range(6)]
    lease = try_reserve_turn(5150)
    assert lease is not None
    try:
        assert (
            asyncio.run(
                dispatch_due(
                    _Bot(),
                    Settings(reminder_batch_size=1, reminder_scan_interval_seconds=1),
                    now_utc=now,
                )
            )
            == 0
        )
    finally:
        release_turn(lease)

    with session_scope() as session:
        attempts = [
            session.get(Reminder, reminder_id).next_attempt_at_utc for reminder_id in reminder_ids
        ]
    assert attempts == [now_epoch + 1] * 4 + [now_epoch] * 2


def test_one_delivery_failure_does_not_orphan_sibling_send(monkeypatch) -> None:
    import laplace.services.reminders as reminder_service

    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    first_owner = _user(5201)
    second_owner = _user(5202)
    first_id = _create(first_owner, epoch_utc(now), "fails")
    second_id = _create(second_owner, epoch_utc(now), "succeeds")
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    class SplitBot:
        async def send_message(self, chat_id: int, text: str):
            if chat_id == 5201:
                raise RuntimeError("provider failed")
            second_started.set()
            await release_second.wait()
            return SimpleNamespace(message_id=222)

    def broken_failure_transition(*_args, **_kwargs):
        raise RuntimeError("database failed")

    monkeypatch.setattr(
        reminder_service,
        "transition_reminder_failure",
        broken_failure_transition,
    )

    async def scenario() -> int:
        task = asyncio.create_task(
            dispatch_due(
                SplitBot(),
                Settings(reminder_max_concurrency=2),
                now_utc=now,
            )
        )
        await second_started.wait()
        await asyncio.sleep(0)
        assert not task.done()
        release_second.set()
        return await task

    assert asyncio.run(scenario()) == 1
    with session_scope() as session:
        assert session.get(Reminder, first_id).status == "pending"
        assert session.get(Reminder, second_id).status == "sent"


def test_memory_reports_total_active_and_only_three_upcoming_reminders() -> None:
    owner = _user(550)
    now = 1_767_268_800
    with session_scope() as session:
        rows = [
            create_reminder(
                session,
                user_id=owner,
                message=f"item {offset}",
                timezone="Asia/Ho_Chi_Minh",
                due_at_utc=now + offset,
            )
            for offset in range(5)
        ]
        rows[-1].status = "cancelled"

    with session_scope() as session:
        user = session.get(User, owner)
        assert user is not None
        stats = session_stats(session, user)

    assert stats["reminders"] == {"total": 5, "active": 4}
    assert stats["recent_reminders"][0]["run_at"].endswith("+07:00")
    assert [item["message"] for item in stats["recent_reminders"]] == [
        "item 0",
        "item 1",
        "item 2",
    ]


def test_scheduler_shutdown_waits_for_active_send_before_provider_close() -> None:
    events: list[str] = []

    class FakeScheduler:
        running = True

        def pause(self) -> None:
            events.append("pause")

        def shutdown(self, *, wait: bool) -> None:
            assert wait is False
            events.append("shutdown")

    async def scenario() -> None:
        async def active_send() -> None:
            await asyncio.sleep(0)
            events.append("send-finished")

        scheduler = ReminderScheduler(_Bot(), Settings())
        scheduler._scheduler = FakeScheduler()
        task = asyncio.create_task(active_send())
        scheduler._active.add(task)
        await scheduler.shutdown()

    asyncio.run(scenario())
    assert events == ["pause", "send-finished", "shutdown"]


def test_permanent_failure_and_ambiguous_exhaustion_are_distinct() -> None:
    class TelegramBadRequest(Exception):
        pass

    owner = _user(606)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    permanent_id = _create(owner, now_epoch, "permanent")
    assert asyncio.run(dispatch_due(_Bot(TelegramBadRequest()), Settings(), now_utc=now)) == 0

    unknown_id = _create(owner, now_epoch, "unknown")
    with session_scope() as session:
        reminder = session.get(Reminder, unknown_id)
        reminder.status = "delivering"
        reminder.attempt_count = 3
    recover_on_startup(Settings(reminder_max_attempts=3), now_utc=now)

    with session_scope() as session:
        assert session.get(Reminder, permanent_id).status == "failed"
        assert session.get(Reminder, unknown_id).status == "delivery_unknown"


def test_startup_recovers_stale_and_marks_too_old_pending_missed() -> None:
    owner = _user(707)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_epoch = epoch_utc(now)
    stale_id = _create(owner, now_epoch - 10, "stale")
    old_id = _create(owner, now_epoch - 120, "old")
    with session_scope() as session:
        stale = session.get(Reminder, stale_id)
        stale.status = "delivering"
        stale.attempt_count = 1

    settings = Settings(reminder_misfire_grace_seconds=60)
    recover_on_startup(settings, now_utc=now)
    bot = _Bot()
    assert asyncio.run(dispatch_due(bot, settings, now_utc=now)) == 1
    with session_scope() as session:
        assert session.get(Reminder, stale_id).status == "sent"
        assert session.get(Reminder, old_id).status == "missed"


def test_forget_refuses_claimed_delivery_and_rolls_back_other_deletes() -> None:
    owner = _user(808)
    due = epoch_utc(datetime(2026, 1, 2, tzinfo=UTC))
    claimed_id = _create(owner, due, "claimed")
    pending_id = _create(owner, due + 1, "pending")
    with session_scope() as session:
        session.get(Reminder, claimed_id).status = "delivering"

    status, result = forget_memory(808)
    assert (status, result) == ("delivery_busy", None)
    with session_scope() as session:
        assert session.get(Reminder, claimed_id) is not None
        assert session.get(Reminder, pending_id) is not None


def test_reminder_survives_engine_restart(_database) -> None:
    owner = _user(9090)
    reminder_id = _create(owner, epoch_utc(datetime(2026, 1, 2, tzinfo=UTC)), "survive")

    reset_engine_for_tests()
    reset_engine_for_tests(_database)
    init_db()
    with session_scope() as session:
        stored = reminder_for_owner(session, owner, reminder_id)
        assert stored is not None
        assert (stored.message, stored.status) == ("survive", "pending")
