"""Khởi chạy Telegram bot bằng long polling (aiogram v3).

Điểm vào duy nhất của bot: đọc token từ Settings, chuẩn bị DB, gắn router và
middleware rồi polling đến khi bị dừng. Token thiếu là lỗi cấu hình nên báo
sớm kèm hướng dẫn thay vì chạy tiếp rồi lỗi khó hiểu.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from laplace.bot.handlers import router
from laplace.bot.middleware import IdentityRateLimitMiddleware
from laplace.config import Settings
from laplace.db import init_db
from laplace.services.reminders import ReminderScheduler

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    """Chạy long polling; đóng session bot khi polling kết thúc hoặc lỗi."""
    settings = Settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "Thiếu Telegram bot token. Tạo bot qua @BotFather trên Telegram, "
            "rồi đặt biến môi trường LAPLACE_TELEGRAM_BOT_TOKEN (hoặc trong .env) "
            "và chạy lại."
        )

    init_db()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.message.middleware(IdentityRateLimitMiddleware())
    dp.include_router(router)
    reminders = ReminderScheduler(bot, settings)

    logger.info("Telegram bot bắt đầu polling...")
    try:
        reminders.start()
        await dp.start_polling(bot)
    finally:
        try:
            await reminders.shutdown()
        finally:
            await bot.session.close()


def main() -> None:
    """Điểm vào sync cho CLI: bọc ``run_bot`` trong ``asyncio.run``."""
    asyncio.run(run_bot())
