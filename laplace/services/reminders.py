"""Owner-safe one-shot reminder normalization, persistence, and delivery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from laplace.config import Settings
from laplace.db import session_scope
from laplace.models import Reminder
from laplace.repo import (
    claim_reminder,
    defer_pending_reminder,
    due_reminder_candidates,
    mark_missed_reminders,
    mark_reminder_sent,
    recover_stale_reminders,
    transition_reminder_failure,
)
from laplace.services.memory import TurnLease, release_turn, try_reserve_turn
from laplace.tools.base import ToolFailure

TERMINAL_STATUSES = frozenset({"sent", "cancelled", "failed", "delivery_unknown", "missed"})
CANDIDATE_SCAN_MULTIPLIER = 4

logger = logging.getLogger(__name__)


class AmbiguousDeliveryError(RuntimeError):
    """Telegram call returned without durable success evidence."""


class ReminderBot(Protocol):
    async def send_message(self, chat_id: int, text: str): ...


@dataclass(frozen=True, slots=True)
class _ClaimedDelivery:
    reminder_id: int
    telegram_user_id: int
    lease: TurnLease
    text: str
    attempts: int
    due_at_utc: int


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_utc(value: datetime) -> int:
    return int(aware_utc(value).timestamp())


def resolve_run_at(
    *,
    now_utc: datetime,
    timezone: str,
    run_at: str | None,
    delay_seconds: int | None,
    max_horizon_days: int,
) -> int:
    """Resolve exactly one absolute or relative user time into UTC epoch seconds."""
    if (run_at is None) == (delay_seconds is None):
        raise ToolFailure("invalid_input", "Provide exactly one of run_at or delay_seconds.")
    now = aware_utc(now_utc)
    if delay_seconds is not None:
        if delay_seconds < 1:
            raise ToolFailure("invalid_input", "delay_seconds must be positive.")
        resolved = now + timedelta(seconds=delay_seconds)
    else:
        assert run_at is not None
        try:
            parsed = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolFailure("invalid_input", "run_at must be a valid RFC3339 timestamp.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ToolFailure("invalid_input", "run_at must include an explicit UTC offset.")
        resolved = parsed.astimezone(UTC)
        expected_offset = resolved.astimezone(ZoneInfo(timezone)).utcoffset()
        if parsed.utcoffset() != expected_offset:
            raise ToolFailure(
                "invalid_input",
                "run_at offset does not match the configured timezone at that instant.",
            )
    if resolved <= now:
        raise ToolFailure("invalid_input", "Reminder time must be in the future.")
    if resolved > now + timedelta(days=max_horizon_days):
        raise ToolFailure("invalid_input", "Reminder time exceeds the configured horizon.")
    return epoch_utc(resolved)


def public_reminder(reminder: Reminder) -> dict[str, object]:
    run_at = datetime.fromtimestamp(reminder.due_at_utc, UTC).isoformat().replace("+00:00", "Z")
    return {
        "id": reminder.id,
        "message": reminder.message,
        "run_at": run_at,
        "status": reminder.status,
    }


def _failure_policy(exc: BaseException, attempts: int) -> tuple[bool, int]:
    """Return retryability and bounded delay without disclosing provider details."""
    name = type(exc).__name__
    if name in {"TelegramForbiddenError", "TelegramUnauthorizedError", "TelegramBadRequest"}:
        return False, 0
    retry_after = getattr(exc, "retry_after", None)
    if name == "TelegramRetryAfter" and isinstance(retry_after, (int, float)):
        return True, max(1, min(300, int(retry_after)))
    retryable = name in {
        "AmbiguousDeliveryError",
        "CancelledError",
        "TelegramRetryAfter",
        "TelegramNetworkError",
        "ClientConnectionError",
        "TimeoutError",
        "TelegramServerError",
        "OSError",
    }
    delay = min(300, 30 * (2 ** max(0, attempts - 1)))
    return retryable, delay


def recover_on_startup(settings: Settings, *, now_utc: datetime | None = None) -> None:
    """Recover rows left delivering by a dead process before polling starts."""
    now = epoch_utc(now_utc or datetime.now(UTC))
    cutoff = now - settings.reminder_misfire_grace_seconds
    with session_scope() as session:
        mark_missed_reminders(session, cutoff)
        recover_stale_reminders(
            session,
            now,
            settings.reminder_max_attempts,
            settings.reminder_misfire_grace_seconds,
        )


async def dispatch_due(
    bot: ReminderBot,
    settings: Settings,
    *,
    now_utc: datetime | None = None,
) -> int:
    """Lease, atomically claim, and deliver one bounded due batch."""
    fixed_now = epoch_utc(now_utc) if now_utc is not None else None

    def clock() -> int:
        return fixed_now if fixed_now is not None else epoch_utc(datetime.now(UTC))

    scan_now = clock()
    cutoff = scan_now - settings.reminder_misfire_grace_seconds
    with session_scope() as session:
        mark_missed_reminders(session, cutoff)

    claimed: list[_ClaimedDelivery] = []
    cursor: tuple[int, int] | None = None
    examined = 0
    max_examined = settings.reminder_batch_size * CANDIDATE_SCAN_MULTIPLIER
    try:
        while len(claimed) < settings.reminder_batch_size and examined < max_examined:
            with session_scope() as session:
                candidates = due_reminder_candidates(
                    session,
                    scan_now,
                    min(settings.reminder_batch_size, max_examined - examined),
                    after=cursor,
                )
            if not candidates:
                break
            for reminder_id, next_attempt_at, telegram_user_id in candidates:
                examined += 1
                cursor = (next_attempt_at, reminder_id)
                lease = try_reserve_turn(telegram_user_id, kind="reminder")
                if lease is None:
                    with session_scope() as session:
                        defer_pending_reminder(
                            session,
                            reminder_id,
                            eligible_at_utc=scan_now,
                            next_attempt_at_utc=max(
                                scan_now + 1,
                                clock() + settings.reminder_scan_interval_seconds,
                            ),
                        )
                    continue

                item: _ClaimedDelivery | None = None
                try:
                    attempt_at = clock()
                    with session_scope() as session:
                        if claim_reminder(session, reminder_id, attempt_at):
                            reminder = session.get(Reminder, reminder_id)
                            if reminder is None:
                                raise RuntimeError("Claimed reminder disappeared")
                            item = _ClaimedDelivery(
                                reminder_id=reminder_id,
                                telegram_user_id=telegram_user_id,
                                lease=lease,
                                text=reminder.message,
                                attempts=reminder.attempt_count,
                                due_at_utc=reminder.due_at_utc,
                            )
                finally:
                    if item is None:
                        release_turn(lease)
                if item is not None:
                    claimed.append(item)
                if len(claimed) >= settings.reminder_batch_size:
                    break
    except BaseException as exc:
        for item in claimed:
            try:
                failure_at = clock()
                with session_scope() as session:
                    transition_reminder_failure(
                        session,
                        item.reminder_id,
                        now_utc=failure_at,
                        error=type(exc).__name__,
                        retry_at_utc=failure_at + settings.reminder_scan_interval_seconds,
                        max_attempts=settings.reminder_max_attempts,
                        grace_seconds=settings.reminder_misfire_grace_seconds,
                        retryable=True,
                    )
            except Exception as cleanup_error:
                logger.error(
                    "Could not requeue pre-send reminder %s",
                    item.reminder_id,
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )
            finally:
                release_turn(item.lease)
        raise

    semaphore = asyncio.Semaphore(settings.reminder_max_concurrency)

    async def deliver(item: _ClaimedDelivery) -> bool:
        def record_failure(exc: BaseException) -> None:
            retryable, delay = _failure_policy(exc, item.attempts)
            failure_at = clock()
            with session_scope() as session:
                transition_reminder_failure(
                    session,
                    item.reminder_id,
                    now_utc=failure_at,
                    error=type(exc).__name__,
                    retry_at_utc=failure_at + delay,
                    max_attempts=settings.reminder_max_attempts,
                    grace_seconds=settings.reminder_misfire_grace_seconds,
                    retryable=retryable,
                )

        try:
            async with semaphore:
                scheduled = (
                    datetime.fromtimestamp(item.due_at_utc, UTC).isoformat().replace("+00:00", "Z")
                )
                rendered = f"Nhắc việc #{item.reminder_id} ({scheduled}): {item.text}"
                if clock() > item.due_at_utc:
                    rendered += "\n(Gửi trễ sau khi dịch vụ phục hồi.)"
                try:
                    sent = await asyncio.wait_for(
                        bot.send_message(
                            chat_id=item.telegram_user_id,
                            text=rendered,
                        ),
                        timeout=settings.reminder_send_timeout_seconds,
                    )
                    message_id = getattr(sent, "message_id", None)
                    if not isinstance(message_id, int):
                        raise AmbiguousDeliveryError("Telegram did not return a message ID")
                    try:
                        with session_scope() as session:
                            if not mark_reminder_sent(
                                session,
                                item.reminder_id,
                                now_utc=clock(),
                                telegram_message_id=message_id,
                            ):
                                raise RuntimeError("Claimed reminder lost delivery ownership")
                    except Exception as exc:
                        raise AmbiguousDeliveryError(
                            "Could not persist Telegram success evidence"
                        ) from exc
                    return True
                except Exception as exc:
                    record_failure(exc)
                    return False
        except asyncio.CancelledError as exc:
            record_failure(exc)
            raise
        finally:
            release_turn(item.lease)

    tasks = [asyncio.create_task(deliver(item)) for item in claimed]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        with session_scope() as session:
            recover_stale_reminders(
                session,
                clock(),
                settings.reminder_max_attempts,
                settings.reminder_misfire_grace_seconds,
            )
        for error in errors:
            logger.error(
                "Reminder delivery task failed: %s",
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )
    return sum(result is True for result in results)


class ReminderScheduler:
    """AsyncIOScheduler lifecycle wrapper with bounded in-flight shutdown."""

    def __init__(self, bot: ReminderBot, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._active: set[asyncio.Task] = set()
        self._stopping = False

    async def _tick(self) -> None:
        if self._stopping:
            return
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            await dispatch_due(self._bot, self._settings)
        finally:
            if task is not None:
                self._active.discard(task)

    def start(self) -> None:
        recover_on_startup(self._settings)
        self._scheduler.add_job(
            self._tick,
            "interval",
            seconds=self._settings.reminder_scan_interval_seconds,
            id="laplace-reminder-dispatch",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._scheduler.start()

    async def shutdown(self) -> None:
        self._stopping = True
        running = self._scheduler.running
        if running:
            self._scheduler.pause()
        active = [task for task in self._active if task is not asyncio.current_task()]
        if active:
            _done, pending = await asyncio.wait(
                active,
                timeout=self._settings.reminder_send_timeout_seconds + 1,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if running:
            self._scheduler.shutdown(wait=False)
