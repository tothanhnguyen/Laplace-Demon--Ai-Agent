"""Khởi chạy Telegram bot bằng long polling (aiogram v3).

Điểm vào duy nhất của bot: đọc token từ Settings, chuẩn bị DB, gắn router và
middleware rồi polling đến khi bị dừng. Token thiếu là lỗi cấu hình nên báo
sớm kèm hướng dẫn thay vì chạy tiếp rồi lỗi khó hiểu.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from laplace import repo
from laplace.bot import handlers
from laplace.bot.handlers import drain_chat_workers, remove_approval_keyboard, router
from laplace.bot.middleware import IdentityRateLimitMiddleware
from laplace.config import Settings
from laplace.db import init_db, session_scope
from laplace.logging import safe_error_code
from laplace.services import chat
from laplace.services.memory import start_chat_admission, stop_chat_admission
from laplace.services.reminders import ReminderScheduler

logger = logging.getLogger(__name__)


def cleanup_orphan_uploads() -> None:
    """Only remove our direct managed files; never traverse a symlink/directory."""
    root = handlers.UPLOAD_DIR
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return
    for path in root.iterdir():
        if not path.name.startswith("laplace-") or path.is_symlink() or not path.is_file():
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Không dọn được upload tạm: %s", safe_error_code(exc))


async def expire_approval_previews(bot: Bot) -> None:
    """One bounded scan per scheduler tick; DB commit precedes keyboard cleanup."""
    for record in chat.expire_paused_turns():
        await remove_approval_keyboard(bot, record)


async def _shutdown(bot: Bot, reminders: ReminderScheduler) -> None:
    """The cleanup task owns draining and transport closure to completion."""
    try:
        reminders.stop_jobs()
        await drain_chat_workers()
        for record in chat.interrupt_paused_turns():
            await remove_approval_keyboard(bot, record)
    finally:
        try:
            await reminders.shutdown()
        finally:
            await bot.session.close()


async def run_bot() -> None:
    """Chạy long polling; đóng session bot khi polling kết thúc hoặc lỗi."""
    stop_chat_admission()
    settings = Settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "Thiếu Telegram bot token. Tạo bot qua @BotFather trên Telegram, "
            "rồi đặt biến môi trường LAPLACE_TELEGRAM_BOT_TOKEN (hoặc trong .env) "
            "và chạy lại."
        )

    init_db()
    interrupted = chat.interrupt_paused_turns()
    with session_scope() as session:
        repo.interrupt_reserved_llm_calls(session)
    cleanup_orphan_uploads()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.message.middleware(IdentityRateLimitMiddleware())
    dp.callback_query.middleware(IdentityRateLimitMiddleware())
    dp.include_router(router)
    reminders = ReminderScheduler(bot, settings, maintenance=lambda: expire_approval_previews(bot))

    logger.info("Telegram bot bắt đầu polling...")
    try:
        start_chat_admission()
        for record in interrupted:
            await remove_approval_keyboard(bot, record)
        reminders.start()
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        stop_chat_admission()
        cleanup = asyncio.create_task(_shutdown(bot, reminders))
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                cancelled = True
                if cleanup.done():
                    cleanup.result()
                    break
        if cancelled:
            raise asyncio.CancelledError


def main() -> None:
    """Điểm vào sync cho CLI: bọc ``run_bot`` trong ``asyncio.run``."""
    asyncio.run(run_bot())
