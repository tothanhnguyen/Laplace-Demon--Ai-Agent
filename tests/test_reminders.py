import asyncio
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import laplace.tools.reminders  # noqa: F401
from laplace import repo
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.base import BudgetStop
from laplace.models import Reminder, Task, ToolApproval, User
from laplace.repo import create_reminder, get_or_create_user, reminder_for_owner, session_stats
from laplace.services.budget import TurnBudget
from laplace.services.memory import (
    forget_memory,
    release_turn,
    request_cancel,
    try_reserve_turn,
)
from laplace.services.reminders import (
    ReminderScheduler,
    dispatch_due,
    epoch_utc,
    recover_on_startup,
)
from laplace.tools.base import (
    ApprovalRequired,
    RiskLevel,
    ToolContext,
    ToolFailure,
    ToolResult,
    execute,
    get_tool,
)
from laplace.tools.reminders import PreparedReminder


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
        budget=TurnBudget(settings=Settings(_env_file=None)),
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


def _stored(reminder_id: int) -> dict[str, object]:
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        assert row is not None
        return {column.name: getattr(row, column.name) for column in Reminder.__table__.columns}


def test_create_proposal_is_normalized_immutable_and_does_not_write() -> None:
    owner = _user(101)
    context = _context(owner, 101)
    created = execute(
        "create_reminder",
        {"message": "  call home\nDestination: someone else  ", "delay_seconds": 60},
        context,
    )
    assert isinstance(created, ApprovalRequired)
    assert created.risk_level is RiskLevel.EXTERNAL_SEND
    assert isinstance(created.proposal.payload, PreparedReminder)
    payload = created.proposal.payload
    assert payload.model_dump() == {
        "operation": "create",
        "reminder_id": None,
        "message_changed": True,
        "time_changed": True,
        "message": "call home\nDestination: someone else",
        "due_at_utc": epoch_utc(context.now_utc) + 60,
        "timezone": "UTC",
        "user_id": owner,
        "destination_telegram_user_id": 101,
        "original_status": None,
        "resource_fingerprint": None,
    }
    assert any("private Telegram owner 101" in fact for fact in created.proposal.preview)
    assert any("2026-01-01T12:01:00Z" in fact for fact in created.proposal.preview)
    assert any(r"call home\nDestination: someone else" in fact for fact in created.proposal.preview)
    assert all("\n" not in fact for fact in created.proposal.preview)
    with pytest.raises(ValidationError):
        payload.due_at_utc = 0
    with pytest.raises(FrozenInstanceError):
        created.proposal.preview = ()
    later = execute(
        "create_reminder",
        {"message": "call home", "delay_seconds": 60},
        replace(context, now_utc=context.now_utc + timedelta(minutes=1)),
    )
    assert isinstance(later, ApprovalRequired)
    assert later.proposal.payload.due_at_utc == payload.due_at_utc + 60
    assert payload.due_at_utc == epoch_utc(context.now_utc) + 60
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


def test_update_and_cancel_proposals_preserve_owner_row() -> None:
    owner = _user(102)
    other = _user(202)
    reminder_id = _create(owner, epoch_utc(_context(owner, 102).now_utc) + 60, "call home")
    before = _stored(reminder_id)
    for name in ("update_reminder", "cancel_reminder"):
        params = {"reminder_id": reminder_id}
        if name == "update_reminder":
            params["message"] = "foreign change"
        hidden = execute(name, params, _context(other, 202))
        missing = execute(name, {**params, "reminder_id": reminder_id + 10}, _context(other, 202))
        assert hidden.error_code == missing.error_code == "not_found"
        assert hidden.error == missing.error
        assert hidden.data is None

    updated = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "  call dad  ", "delay_seconds": 120},
        _context(owner, 102),
    )
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 102))
    assert isinstance(updated, ApprovalRequired)
    assert isinstance(cancelled, ApprovalRequired)
    payload = updated.proposal.payload
    assert (payload.operation, payload.message, payload.due_at_utc) == (
        "update", "call dad", epoch_utc(_context(owner, 102).now_utc) + 120,
    )
    assert payload.message_changed and payload.time_changed
    assert payload.original_status == "pending"
    assert (payload.user_id, payload.destination_telegram_user_id) == (owner, 102)
    assert any("call home" in fact for fact in updated.proposal.preview)
    assert any("call dad" in fact for fact in updated.proposal.preview)
    assert any("12:01:00Z" in fact for fact in updated.proposal.preview)
    assert any("12:02:00Z" in fact for fact in updated.proposal.preview)
    assert cancelled.risk_level is RiskLevel.WRITE_REVERSIBLE
    cancel_payload = cancelled.proposal.payload
    assert cancel_payload.operation == "cancel"
    assert not cancel_payload.message_changed and not cancel_payload.time_changed
    assert cancel_payload.message == "call home"
    assert cancel_payload.due_at_utc == before["due_at_utc"]
    assert cancel_payload.resource_fingerprint == payload.resource_fingerprint
    assert _stored(reminder_id) == before


def test_text_only_update_keeps_overdue_time_timezone_and_retry_state() -> None:
    owner = _user(103)
    due = epoch_utc(_context(owner, 103).now_utc) - 10
    reminder_id = _create(owner, due, "before")
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        row.timezone = "Asia/Ho_Chi_Minh"
        row.next_attempt_at_utc = due + 120
        row.attempt_count = 2
    before = _stored(reminder_id)
    updated = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "message": "  after  "},
        _context(owner, 103),
    )
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 103))
    assert isinstance(updated, ApprovalRequired)
    assert isinstance(cancelled, ApprovalRequired)
    payload = updated.proposal.payload
    assert payload.message == "after" and payload.message_changed
    assert not payload.time_changed
    assert (payload.due_at_utc, payload.timezone) == (due, "Asia/Ho_Chi_Minh")
    assert _stored(reminder_id) == before


def test_explicit_same_time_is_distinct_from_text_only_update() -> None:
    owner = _user(104)
    reminder_id = _create(owner, epoch_utc(_context(owner, 104).now_utc) + 120)
    before = _stored(reminder_id)
    outcome = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "run_at": "2026-01-01T12:02:00Z"},
        _context(owner, 104),
    )
    assert isinstance(outcome, ApprovalRequired)
    assert outcome.proposal.payload.time_changed
    assert not outcome.proposal.payload.message_changed
    assert outcome.proposal.payload.due_at_utc == before["due_at_utc"]
    assert _stored(reminder_id) == before


def test_absolute_time_uses_trusted_timezone() -> None:
    owner = _user(105)
    outcome = execute(
        "create_reminder",
        {"message": "local", "run_at": "2026-01-01T19:02:00+07:00"},
        replace(_context(owner, 105), timezone="Asia/Ho_Chi_Minh"),
    )
    assert isinstance(outcome, ApprovalRequired)
    assert outcome.proposal.payload.due_at_utc == epoch_utc(_context(owner, 105).now_utc) + 120
    assert outcome.proposal.payload.timezone == "Asia/Ho_Chi_Minh"


@pytest.mark.parametrize("name", ["create_reminder", "update_reminder", "cancel_reminder"])
@pytest.mark.parametrize("identity", ["missing", "mismatched"])
def test_owner_identity_must_exist_and_match_before_proposal(name: str, identity: str) -> None:
    owner = _user(106)
    reminder_id = _create(
        owner, epoch_utc(_context(owner, 106).now_utc) + 60, "owner-only-message-106",
    )
    params = {"message": "new", "delay_seconds": 60} if name == "create_reminder" else {
        "reminder_id": reminder_id,
    }
    if name == "update_reminder":
        params["message"] = "new"
    context = _context(owner + 999 if identity == "missing" else owner, 999)
    before = _stored(reminder_id)
    outcome = execute(name, params, context)
    assert outcome.error_code == "forbidden"
    assert outcome.data is None
    assert "owner-only-message-106" not in outcome.error
    assert _stored(reminder_id) == before


@pytest.mark.parametrize("name", ["create_reminder", "update_reminder", "cancel_reminder"])
@pytest.mark.parametrize("field", [
    "user_id", "telegram_user_id", "destination", "destination_telegram_user_id",
    "risk_level", "risk", "approval_id", "approved", "path", "due_at_utc", "prepared",
    "timezone", "transport", "resource_fingerprint",
])
def test_public_schema_rejects_privileged_fields(name: str, field: str) -> None:
    owner = _user(107)
    params = {"message": "new", "delay_seconds": 60} if name == "create_reminder" else {
        "reminder_id": 1,
    }
    if name == "update_reminder":
        params["message"] = "new"
    outcome = execute(name, {**params, field: "crafted"}, _context(owner, 107))
    assert outcome.error_code == "invalid_input"
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


@pytest.mark.parametrize("name", ["create_reminder", "update_reminder", "cancel_reminder"])
def test_direct_run_requires_durable_consent_and_never_mutates(name: str) -> None:
    owner = _user(108)
    reminder_id = _create(owner, epoch_utc(_context(owner, 108).now_utc) + 60)
    before = _stored(reminder_id)
    params = {"message": "new", "delay_seconds": 60} if name == "create_reminder" else {
        "reminder_id": reminder_id,
    }
    if name == "update_reminder":
        params["message"] = "new"
    spec = get_tool(name)
    assert spec is not None
    with pytest.raises(ToolFailure) as failure:
        spec.run(spec.args_model.model_validate(params), _context(owner, 108))
    assert failure.value.error_code == "forbidden"
    assert _stored(reminder_id) == before
    with session_scope() as session:
        assert [row.id for row in session.scalars(select(Reminder)).all()] == [reminder_id]


def test_resource_fingerprint_ignores_scheduler_bookkeeping_but_tracks_resource() -> None:
    owner = _user(109)
    reminder_id = _create(owner, epoch_utc(_context(owner, 109).now_utc) + 60, "original")
    params = {"reminder_id": reminder_id, "delay_seconds": 120}

    def fingerprint() -> str:
        outcome = execute("update_reminder", params, _context(owner, 109))
        assert isinstance(outcome, ApprovalRequired)
        assert outcome.proposal.payload.resource_fingerprint is not None
        return outcome.proposal.payload.resource_fingerprint

    original = fingerprint()
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        row.next_attempt_at_utc += 30
        row.updated_at += timedelta(seconds=30)
    assert fingerprint() == original
    for field, value in (
        ("message", "changed"), ("due_at_utc", epoch_utc(_context(owner, 109).now_utc) + 90),
        ("timezone", "Asia/Ho_Chi_Minh"), ("status", "failed"),
    ):
        with session_scope() as session:
            setattr(session.get(Reminder, reminder_id), field, value)
        changed = fingerprint()
        assert changed != original
        original = changed


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
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


@pytest.mark.parametrize("status", ["failed", "delivery_unknown"])
def test_failed_or_unknown_requires_future_time_to_rearm(status: str) -> None:
    owner = _user(304)
    reminder_id = _create(owner, epoch_utc(datetime(2026, 1, 2, tzinfo=UTC)))
    with session_scope() as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = status
        reminder.attempt_count = 3
        reminder.last_error = "provider evidence"
    before = _stored(reminder_id)
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
    past_time = execute(
        "update_reminder",
        {"reminder_id": reminder_id, "run_at": "2026-01-01T11:59:00Z"},
        _context(owner, 304),
    )
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 304))
    assert past_time.error_code == cancelled.error_code == "invalid_input"
    assert message_only.error_code == "invalid_input"
    assert isinstance(rearmed, ApprovalRequired)
    payload = rearmed.proposal.payload
    assert payload.time_changed
    assert payload.original_status == status
    assert payload.message == "new"
    assert payload.due_at_utc == epoch_utc(_context(owner, 304).now_utc) + 120
    assert _stored(reminder_id) == before


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


@pytest.mark.parametrize("status", ["sent", "cancelled", "missed", "delivering"])
def test_terminal_or_claimed_reminder_rejects_reschedule_and_cancel(status: str) -> None:
    owner = _user(910)
    reminder_id = _create(owner, epoch_utc(_context(owner, 910).now_utc) + 60, "original")
    with session_scope() as session:
        session.get(Reminder, reminder_id).status = status
    before = _stored(reminder_id)
    for params in (
        {"reminder_id": reminder_id, "message": "changed"},
        {"reminder_id": reminder_id, "delay_seconds": 120},
    ):
        outcome = execute("update_reminder", params, _context(owner, 910))
        assert outcome.error_code == "invalid_input"
    cancelled = execute("cancel_reminder", {"reminder_id": reminder_id}, _context(owner, 910))
    assert cancelled.error_code == "invalid_input"
    assert _stored(reminder_id) == before


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


def _task_context(user_id: int, telegram_id: int) -> ToolContext:
    with session_scope() as session:
        owner = session.get(User, user_id)
        conversation = repo.get_or_create_conversation(session, owner)
        request = repo.add_message(session, conversation, "user", "manage my reminder")
        task = repo.create_task(
            session,
            user_id=user_id,
            conversation_id=conversation.id,
            request_message_id=request.id,
            goal="manage reminder",
        )
        return replace(
            _context(user_id, telegram_id),
            conversation_id=conversation.id,
            request_message_id=request.id,
            task_id=task.id,
        )


def _consent(name: str, params: dict, context: ToolContext) -> ToolContext:
    from laplace.services.approvals import bind_preview, claim_approval, create_pending

    proposal = execute(name, params, context)
    assert isinstance(proposal, ApprovalRequired)
    now = epoch_utc(context.now_utc)
    pause = create_pending(proposal, chat_id=context.telegram_user_id, now_utc=now)
    assert pause is not None
    assert bind_preview(
        pause.token, telegram_user_id=context.telegram_user_id,
        chat_id=context.telegram_user_id, message_id=77, now_utc=now,
    )
    claimed = claim_approval(
        pause.token, telegram_user_id=context.telegram_user_id,
        chat_id=context.telegram_user_id, message_id=77, now_utc=now,
    )
    assert claimed is not None and claimed.status == "approved"
    return replace(context, approval_id=pause.approval_id, approval_token=pause.token)


def _approval_state(context: ToolContext) -> tuple[str, int | None, str | None]:
    with session_scope() as session:
        row = session.get(ToolApproval, context.approval_id)
        return row.status, row.consumed_at, row.result_json


def _clock(monkeypatch, now: int) -> None:
    import laplace.services.approvals as approvals

    monkeypatch.setattr(approvals, "epoch_now", lambda: now)


def test_approved_create_uses_frozen_relative_time_and_atomic_exact_receipt(monkeypatch) -> None:
    owner = _user(9201)
    context = _task_context(owner, 9201)
    params = {"message": "  gọi mẹ 😀\nline two  ", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    now = epoch_utc(context.now_utc) + 60
    _clock(monkeypatch, now)
    receipt = execute(
        "create_reminder", params, replace(approved, now_utc=context.now_utc + timedelta(days=1))
    )
    assert isinstance(receipt, ToolResult) and receipt.ok
    row = _stored(receipt.data["id"])
    assert row["message"] == "gọi mẹ 😀\nline two"
    assert row["due_at_utc"] == epoch_utc(context.now_utc) + 120
    status, consumed, result_json = _approval_state(approved)
    assert (status, consumed) == ("executed", now)
    assert ToolResult.model_validate_json(result_json) == receipt
    replay = execute("create_reminder", params, approved)
    assert not replay.ok and replay.error_code == "forbidden"
    with session_scope() as session:
        assert session.scalars(select(Reminder.id)).all() == [receipt.data["id"]]
        assert session.get(Task, context.task_id).status == "running"
    assert _approval_state(approved) == (status, consumed, result_json)


def test_approved_text_update_and_cancel_keep_overdue_schedule_bookkeeping(monkeypatch) -> None:
    owner = _user(9202)
    context = _task_context(owner, 9202)
    due = epoch_utc(context.now_utc) - 30
    reminder_id = _create(owner, due, "before")
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        row.timezone = "Asia/Ho_Chi_Minh"
        row.next_attempt_at_utc = due + 400
        row.attempt_count = 2
        row.first_attempt_at_utc = due - 120
        row.last_attempt_at_utc = due - 60
        row.last_error = "retry evidence"
    before = _stored(reminder_id)
    params = {"reminder_id": reminder_id, "message": "  after  "}
    approved = _consent("update_reminder", params, context)
    # Dispatcher deferrals are not material resource changes.
    with session_scope() as session:
        session.get(Reminder, reminder_id).next_attempt_at_utc += 10
    _clock(monkeypatch, epoch_utc(context.now_utc) + 60)
    receipt = execute("update_reminder", params, approved)
    assert receipt.ok and receipt.data["message"] == "after"
    after = _stored(reminder_id)
    for field in (
        "due_at_utc", "timezone", "attempt_count", "first_attempt_at_utc",
        "last_attempt_at_utc", "last_error",
    ):
        assert after[field] == before[field]
    assert after["next_attempt_at_utc"] == before["next_attempt_at_utc"] + 10
    assert ToolResult.model_validate_json(_approval_state(approved)[2]) == receipt
    cancel_context = _task_context(owner, 9202)
    cancel_params = {"reminder_id": reminder_id}
    cancelled = _consent("cancel_reminder", cancel_params, cancel_context)
    cancel_receipt = execute("cancel_reminder", cancel_params, cancelled)
    assert cancel_receipt.ok and cancel_receipt.data["status"] == "cancelled"
    assert ToolResult.model_validate_json(_approval_state(cancelled)[2]) == cancel_receipt
    final = _stored(reminder_id)
    assert final["status"] == "cancelled"
    assert final["due_at_utc"] == due
    assert final["next_attempt_at_utc"] == after["next_attempt_at_utc"]


@pytest.mark.parametrize("status", ["pending", "failed", "delivery_unknown"])
def test_approved_reschedule_resets_delivery_state_without_reresolving(status, monkeypatch) -> None:
    owner = _user(9203)
    context = _task_context(owner, 9203)
    reminder_id = _create(owner, epoch_utc(context.now_utc) - 10, "original")
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        row.status = status
        row.attempt_count = 3
        row.first_attempt_at_utc = epoch_utc(context.now_utc) - 90
        row.last_attempt_at_utc = epoch_utc(context.now_utc) - 40
        row.last_error = "delivery evidence"
    params = {"reminder_id": reminder_id, "delay_seconds": 120}
    approved = _consent("update_reminder", params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc) + 30)
    result = execute("update_reminder", params, approved)
    assert result.ok
    row = _stored(reminder_id)
    assert row["message"] == "original"
    assert row["due_at_utc"] == row["next_attempt_at_utc"] == epoch_utc(context.now_utc) + 120
    assert row["status"] == "pending" and row["attempt_count"] == 0
    assert row["first_attempt_at_utc"] is row["last_attempt_at_utc"] is row["last_error"] is None
    assert ToolResult.model_validate_json(_approval_state(approved)[2]) == result


@pytest.mark.parametrize("binding", [
    "token", "approval_id", "task_id", "step_index", "conversation_id",
    "request_message_id", "telegram_user_id", "user_id", "public_args",
    "tool_name", "risk_level", "destination", "policy", "params", "hash",
    "prepared_owner", "prepared_destination", "prepared_fingerprint",
])
def test_approved_write_denies_wrong_durable_binding_without_effect(binding, monkeypatch) -> None:
    owner = _user(9204)
    other = _user(9205)
    context = _task_context(owner, 9204)
    params = {"message": "immutable action", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    approval_id = approved.approval_id
    _clock(monkeypatch, epoch_utc(context.now_utc))
    if binding in {
        "token", "approval_id", "task_id", "step_index", "conversation_id",
        "request_message_id", "telegram_user_id", "user_id",
    }:
        field = "approval_token" if binding == "token" else binding
        value = "another-generation" if binding == "token" else getattr(approved, field) + 10
        if binding == "user_id":
            value = other
        approved = replace(approved, **{field: value})
    elif binding == "public_args":
        params = {**params, "message": "different action"}
    else:
        with session_scope() as session:
            row = session.get(ToolApproval, approved.approval_id)
            if binding == "tool_name":
                row.tool_name = "update_reminder"
            elif binding == "risk_level":
                row.risk_level = "WRITE_REVERSIBLE"
            elif binding == "destination":
                row.chat_id = 9205
            elif binding == "policy":
                row.preview_json = '{"policy_version":2}'
            elif binding == "hash":
                row.public_args_hash = "é" * 64
            else:
                frozen = json.loads(row.params_json)
                if binding == "prepared_owner":
                    frozen["user_id"] = other
                elif binding == "prepared_destination":
                    frozen["destination_telegram_user_id"] = 9205
                elif binding == "prepared_fingerprint":
                    frozen["resource_fingerprint"] = "a" * 64
                else:
                    frozen["message"] = "tampered private action"
                row.params_json = json.dumps(frozen)
    result = execute("create_reminder", params, approved)
    assert not result.ok and result.error_code == "forbidden"
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []
        row = session.get(ToolApproval, approval_id)
        assert row.status == "approved" and row.consumed_at is None and row.result_json is None


@pytest.mark.parametrize("change", ["message", "due_at_utc", "timezone", "status", "deleted"])
@pytest.mark.parametrize("name", ["update_reminder", "cancel_reminder"])
def test_approved_target_change_invalidates_without_overwriting(change, name, monkeypatch) -> None:
    owner = _user(9206)
    context = _task_context(owner, 9206)
    reminder_id = _create(owner, epoch_utc(context.now_utc) + 120, "original")
    params = {"reminder_id": reminder_id}
    if name == "update_reminder":
        params["message"] = "approved update"
    approved = _consent(name, params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    with session_scope() as session:
        row = session.get(Reminder, reminder_id)
        if change == "deleted":
            session.delete(row)
        else:
            setattr(row, change, {
                "message": "external update",
                "due_at_utc": epoch_utc(context.now_utc) + 180,
                "timezone": "Asia/Ho_Chi_Minh",
                "status": "delivering",
            }[change])
    before = None if change == "deleted" else _stored(reminder_id)
    result = execute(name, params, approved)
    assert not result.ok
    assert result.error_code == ("not_found" if change == "deleted" else "invalid_input")
    if before is not None:
        assert _stored(reminder_id) == before
    assert _approval_state(approved) == ("approved", None, None)


@pytest.mark.parametrize("deadline", ["approval", "due"])
def test_approved_write_rechecks_live_clock_at_mutation_boundary(deadline, monkeypatch) -> None:
    import laplace.services.approvals as approvals

    owner = _user(9207)
    context = _task_context(owner, 9207)
    params = {"message": "boundary", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    with session_scope() as session:
        row = session.get(ToolApproval, approved.approval_id)
        expires = row.expires_at
    times = iter([
        epoch_utc(context.now_utc),
        expires if deadline == "approval" else epoch_utc(context.now_utc) + 120,
    ])
    monkeypatch.setattr(approvals, "epoch_now", lambda: next(times))
    result = execute("create_reminder", params, approved)
    assert not result.ok and result.error_code == "invalid_input"
    assert _approval_state(approved) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


def test_actual_chat_cancellation_immediately_before_write_has_no_effect(monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9208)
    context = _task_context(owner, 9208)
    params = {"message": "must not write", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    lease = try_reserve_turn(9208)
    assert lease is not None
    original = reminder_tools._load_approved

    def load_then_cancel(*args, **kwargs):
        loaded = original(*args, **kwargs)
        assert request_cancel(9208)
        return loaded

    monkeypatch.setattr(reminder_tools, "_load_approved", load_then_cancel)
    try:
        result = execute("create_reminder", params, approved)
    finally:
        release_turn(lease)
    assert not result.ok and result.error_code == "forbidden"
    assert _approval_state(approved) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


@pytest.mark.parametrize("name", ["create_reminder", "update_reminder", "cancel_reminder"])
def test_output_validation_failure_rolls_back_effect_and_receipt(name, monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9209)
    context = _task_context(owner, 9209)
    reminder_id = _create(owner, epoch_utc(context.now_utc) + 120, "before")
    params = {"message": "new", "delay_seconds": 120} if name == "create_reminder" else {
        "reminder_id": reminder_id,
    }
    if name == "update_reminder":
        params["message"] = "new"
    approved = _consent(name, params, context)
    before = _stored(reminder_id)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    monkeypatch.setattr(reminder_tools, "public_reminder", lambda row: {
        "id": row.id, "message": row.message, "run_at": 123, "status": row.status,
    })
    result = execute(name, params, approved)
    assert not result.ok
    assert _stored(reminder_id) == before
    assert _approval_state(approved) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder.id)).all() == [reminder_id]


def test_approval_preview_keeps_unicode_newlines_and_actual_local_old_new_times() -> None:
    owner = _user(9210)
    context = replace(_task_context(owner, 9210), timezone="Asia/Ho_Chi_Minh")
    reminder_id = _create(owner, epoch_utc(context.now_utc) + 120, "cũ 😀\nold")
    with session_scope() as session:
        session.get(Reminder, reminder_id).timezone = "Asia/Ho_Chi_Minh"
    outcome = execute("update_reminder", {
        "reminder_id": reminder_id, "message": "mới 😀\nnew", "delay_seconds": 240,
    }, context)
    assert isinstance(outcome, ApprovalRequired)
    facts = outcome.proposal.preview
    assert any(json.dumps("cũ 😀\nold", ensure_ascii=False) in fact for fact in facts)
    assert any(json.dumps("mới 😀\nnew", ensure_ascii=False) in fact for fact in facts)
    assert all("\n" not in fact for fact in facts)
    assert any("2026-01-01T19:02:00+07:00" in fact and "12:02:00Z" in fact for fact in facts)
    assert any("2026-01-01T19:04:00+07:00" in fact and "12:04:00Z" in fact for fact in facts)


def test_old_token_cannot_authorize_reused_sqlite_approval_id_after_forget(monkeypatch) -> None:
    owner = _user(9211)
    context = _task_context(owner, 9211)
    params = {"message": "new generation only", "delay_seconds": 120}
    old = _consent("create_reminder", params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    with session_scope() as session:
        repo.wipe_session(session, session.get(User, owner))
    new = _consent("create_reminder", params, _task_context(owner, 9211))
    assert new.approval_id == old.approval_id
    assert new.approval_token != old.approval_token
    stale = execute("create_reminder", params, old)
    assert not stale.ok and stale.error_code == "forbidden"
    assert _approval_state(new) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []
    receipt = execute("create_reminder", params, new)
    assert receipt.ok
    with session_scope() as session:
        assert session.scalars(select(Reminder.id)).all() == [receipt.data["id"]]


@pytest.mark.parametrize("name", ["update_reminder", "cancel_reminder"])
def test_resource_change_between_recheck_and_write_loses_effect_cas(name, monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9212)
    context = _task_context(owner, 9212)
    reminder_id = _create(owner, epoch_utc(context.now_utc) + 120, "original")
    params = {"reminder_id": reminder_id}
    if name == "update_reminder":
        params["message"] = "approved text"
    approved = _consent(name, params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    original = reminder_tools._load_approved

    def load_then_change(*args, **kwargs):
        loaded = original(*args, **kwargs)
        with session_scope() as session:
            session.get(Reminder, reminder_id).message = "concurrent edit"
        return loaded

    monkeypatch.setattr(reminder_tools, "_load_approved", load_then_change)
    result = execute(name, params, approved)
    assert not result.ok and result.error_code == "invalid_input"
    assert _stored(reminder_id)["message"] == "concurrent edit"
    assert _stored(reminder_id)["status"] == "pending"
    assert _approval_state(approved) == ("approved", None, None)


def test_revoked_approval_during_t3_rolls_back_written_effect(monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools
    from laplace.services.approvals import terminate_approval

    owner = _user(9213)
    context = _task_context(owner, 9213)
    params = {"message": "revoked before commit", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    original = reminder_tools._load_approved

    def load_then_revoke(*args, **kwargs):
        loaded = original(*args, **kwargs)
        revoked = terminate_approval(
            approved.approval_token, status="cancelled", telegram_user_id=9213,
            allowed_statuses=("approved",),
        )
        assert revoked is not None
        return loaded

    monkeypatch.setattr(reminder_tools, "_load_approved", load_then_revoke)
    result = execute("create_reminder", params, approved)
    assert not result.ok and result.error_code == "forbidden"
    assert _approval_state(approved) == ("cancelled", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []
        assert session.get(Task, context.task_id).status == "cancelled"


@pytest.mark.parametrize("name", ["create_reminder", "update_reminder", "cancel_reminder"])
@pytest.mark.parametrize("boundary", ["before_write", "before_consent"])
def test_budget_deadline_rolls_back_effect_and_preserves_consent(
    name, boundary, monkeypatch
) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9214)
    elapsed = [0.0]
    context = replace(
        _task_context(owner, 9214),
        budget=TurnBudget(
            settings=Settings(_env_file=None, max_seconds=1), clock=lambda: elapsed[0]
        ),
    )
    reminder_id = _create(owner, epoch_utc(context.now_utc) + 120, "before")
    params = {"message": "new", "delay_seconds": 120} if name == "create_reminder" else {
        "reminder_id": reminder_id,
    }
    if name == "update_reminder":
        params["message"] = "new"
    approved = _consent(name, params, context)
    before = _stored(reminder_id)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    hook = "_load_approved" if boundary == "before_write" else "public_reminder"
    original = getattr(reminder_tools, hook)

    def expire_after_work(*args, **kwargs):
        value = original(*args, **kwargs)
        elapsed[0] = 1.0
        return value

    monkeypatch.setattr(reminder_tools, hook, expire_after_work)
    with pytest.raises(BudgetStop) as stopped:
        execute(name, params, approved)
    assert stopped.value.reason == "time_limit"
    assert _stored(reminder_id) == before
    assert _approval_state(approved) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder.id)).all() == [reminder_id]


def test_actual_cancellation_before_consent_rolls_back_written_effect(monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9215)
    context = _task_context(owner, 9215)
    params = {"message": "cancel before consent", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    _clock(monkeypatch, epoch_utc(context.now_utc))
    lease = try_reserve_turn(9215)
    assert lease is not None
    original = reminder_tools.public_reminder

    def cancel_after_write(row):
        result = original(row)
        assert request_cancel(9215)
        return result

    monkeypatch.setattr(reminder_tools, "public_reminder", cancel_after_write)
    try:
        result = execute("create_reminder", params, approved)
    finally:
        release_turn(lease)
    assert not result.ok and result.error_code == "forbidden"
    assert _approval_state(approved) == ("approved", None, None)
    with session_scope() as session:
        assert session.scalars(select(Reminder)).all() == []


def test_late_budget_stop_retains_committed_reminder_and_exact_receipt(monkeypatch) -> None:
    import laplace.tools.reminders as reminder_tools

    owner = _user(9216)
    elapsed = [0.0]
    context = replace(
        _task_context(owner, 9216),
        budget=TurnBudget(
            settings=Settings(_env_file=None, max_seconds=1), clock=lambda: elapsed[0]
        ),
    )
    params = {"message": "committed before stop", "delay_seconds": 120}
    approved = _consent("create_reminder", params, context)
    now = epoch_utc(context.now_utc)
    _clock(monkeypatch, now)

    @contextmanager
    def commit_then_expire(*, timeout_seconds):
        with session_scope(timeout_seconds=timeout_seconds) as session:
            yield session
        elapsed[0] = 1.0

    monkeypatch.setattr(reminder_tools, "session_scope", commit_then_expire)
    with pytest.raises(BudgetStop) as stopped:
        execute("create_reminder", params, approved)
    assert stopped.value.reason == "time_limit"
    status, consumed, result_json = _approval_state(approved)
    assert (status, consumed) == ("executed", now)
    receipt = ToolResult.model_validate_json(result_json)
    assert receipt.ok and receipt.data["message"] == "committed before stop"
    row = _stored(receipt.data["id"])
    assert (row["message"], row["status"]) == ("committed before stop", "pending")
    assert row["due_at_utc"] == now + 120
    with session_scope() as session:
        assert session.scalars(select(Reminder.id)).all() == [receipt.data["id"]]
