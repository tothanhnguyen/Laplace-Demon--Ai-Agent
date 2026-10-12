import asyncio
from types import SimpleNamespace

import pytest

from laplace.bot import runner


class _MessagePipeline:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def middleware(self, _middleware) -> None:
        self.events.append("middleware")


class _Dispatcher:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.message = _MessagePipeline(events)

    def include_router(self, _router) -> None:
        self.events.append("router")

    async def start_polling(self, _bot) -> None:
        self.events.append("polling")
        if self.error is not None:
            raise self.error


class _BotSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("bot-close")


class _Bot:
    def __init__(self, events: list[str]) -> None:
        self.session = _BotSession(events)


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

    async def shutdown(self) -> None:
        self.events.append("scheduler-stop")
        if self.error is not None:
            raise self.error


def _install(
    monkeypatch,
    *,
    polling_error: Exception | None = None,
    scheduler_error: Exception | None = None,
) -> list[str]:
    events: list[str] = []
    bot = _Bot(events)
    dispatcher = _Dispatcher(events, polling_error)
    monkeypatch.setattr(
        runner,
        "Settings",
        lambda: SimpleNamespace(telegram_bot_token="token"),
    )
    monkeypatch.setattr(runner, "init_db", lambda: events.append("init-db"))
    monkeypatch.setattr(runner, "Bot", lambda token: bot)
    monkeypatch.setattr(runner, "Dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        runner,
        "ReminderScheduler",
        lambda bot, settings: _Scheduler(bot, settings, events, scheduler_error),
    )
    return events


def test_runner_stops_scheduler_before_bot_close(monkeypatch) -> None:
    events = _install(monkeypatch)
    asyncio.run(runner.run_bot())
    assert events == [
        "init-db",
        "middleware",
        "router",
        "scheduler-start",
        "polling",
        "scheduler-stop",
        "bot-close",
    ]


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
        lambda: SimpleNamespace(telegram_bot_token=""),
    )
    touched = []
    monkeypatch.setattr(runner, "init_db", lambda: touched.append("db"))
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        asyncio.run(runner.run_bot())
    assert touched == []
