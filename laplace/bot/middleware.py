"""Middleware aiogram: chuẩn hóa identity và chặn rate limit trước handler.

Middleware chỉ làm hai việc ở boundary: gắn ``telegram_user_id``/``username``
vào ``data`` để handler không phải tự móc ``event.from_user``, và từ chối
lịch sự khi user vượt quota. Việc map user với phiên hội thoại nằm ở tầng
service/DB, không thuộc trách nhiệm middleware.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from laplace.bot.ratelimit import TokenBucket


class IdentityRateLimitMiddleware(BaseMiddleware):
    """Gắn identity vào ``data`` và áp token bucket cho message tốn tài nguyên.

    Lệnh (text bắt đầu bằng ``/``) không bị tính quota vì chúng rẻ và người
    dùng đang bị chặn vẫn cần ``/cancel`` hay ``/status``; chỉ tin nhắn tạo
    việc cho agent (văn bản thường, tài liệu) mới trừ token.
    """

    def __init__(self, bucket: TokenBucket | None = None) -> None:
        self.bucket = bucket or TokenBucket()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Bổ sung identity, kiểm tra quota rồi mới chuyển tiếp cho handler."""
        # Bỏ qua update không có người gửi
        if not isinstance(event, Message) or event.from_user is None:
            return None

        # Gắn identity vào data
        user_id = event.from_user.id
        data["telegram_user_id"] = user_id
        data["username"] = event.from_user.username

        # Lệnh / miễn quota; chỉ tin thường mới tính rate limit
        is_command = bool(event.text and event.text.startswith("/"))
        if not is_command and not self.bucket.allow(user_id):
            wait_s = int(self.bucket.retry_after(user_id))
            await event.answer(
                f"Bạn đang gửi hơi nhanh. Vui lòng chờ khoảng {wait_s} giây rồi thử lại nhé."
            )
            return None

        return await handler(event, data)
