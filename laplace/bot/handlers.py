"""Handlers Telegram (aiogram v3): lệnh cơ bản và luồng chat có báo tiến độ.

Mỗi tin nhắn văn bản chạy ``laplace.services.chat.handle_message`` trong
thread riêng qua ``asyncio.to_thread`` để không chặn event loop; tiến độ được
đẩy ngược về event loop bằng ``loop.call_soon_threadsafe`` và edit vào một
message trạng thái duy nhất. Task đang chạy được ghi vào registry theo user
để ``/cancel`` hủy được. Tài liệu gửi lên chỉ được tải về ``var/uploads/``;
tóm tắt nội dung tài liệu thuộc sprint sau.
"""

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from laplace.bot.textsplit import split_message
from laplace.db import session_scope
from laplace.repo import get_or_create_user, user_usage
from laplace.services.chat import handle_message

logger = logging.getLogger(__name__)

router = Router(name="laplace-bot")

PROCESSING_TEXT = "Đang xử lý..."
UPLOAD_DIR = Path("var/uploads")
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10MB

# Lượt xử lý đang chạy của từng user — /cancel tra cứu ở đây để hủy.
_running_tasks: dict[int, asyncio.Task] = {}

START_TEXT = (
    "Xin chào! Mình là Laplace's Demon — trợ lý chat chạy trên LLM.\n\n"
    "Cứ nhắn nội dung bạn cần, mình sẽ trả lời và báo tiến độ trong lúc xử lý.\n"
    "Các lệnh hỗ trợ:\n"
    "/help — hướng dẫn sử dụng\n"
    "/status — thống kê lượt gọi LLM, tokens và chi phí của bạn\n"
    "/cancel — hủy lượt xử lý đang chạy"
)

HELP_TEXT = (
    "Cách dùng:\n"
    "- Nhắn văn bản bất kỳ để hỏi; mình xử lý từng lượt một cho mỗi người.\n"
    "- /status xem số lời gọi LLM, tokens và chi phí đã dùng.\n"
    "- /cancel hủy lượt đang xử lý nếu chờ quá lâu.\n"
    "- Gửi tài liệu (tối đa 10MB) để lưu lại; tính năng tóm tắt tài liệu "
    "sẽ có ở sprint sau."
)


class _StatusEditor:
    """Edit một message trạng thái duy nhất, nhận text từ thread khác an toàn.

    Callback tiến độ chạy trong worker thread nên không được đụng API aiogram
    trực tiếp; ``push_threadsafe`` chuyển text về event loop, chỉ giữ bản mới
    nhất và edit tuần tự để không dồn đống request khi tiến độ dồn dập.
    """

    def __init__(self, status: Message, loop: asyncio.AbstractEventLoop) -> None:
        # Giữ reference tới message trạng thái và event loop
        self._status = status
        self._loop = loop
        self._latest: str | None = None
        self._draining = False

    def push_threadsafe(self, text: str) -> None:
        # Chuyển text từ worker thread về event loop an toàn
        """Được gọi từ worker thread: đẩy text mới về event loop."""
        self._loop.call_soon_threadsafe(self._push, text)

    def _push(self, text: str) -> None:
        self._latest = text
        if not self._draining:
            self._draining = True
            self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        # Lấy text mới nhất và edit message (gộp nếu dồn dập)
        try:
            while self._latest is not None:
                text = self._latest
                self._latest = None
                await self.edit(text)
        finally:
            self._draining = False

    async def edit(self, text: str) -> None:
        """Edit message trạng thái; lỗi edit (trùng nội dung...) chỉ ghi log."""
        try:
            await self._status.edit_text(text)
        except Exception:  # noqa: BLE001 — tiến độ là phụ, không được làm vỡ luồng chính
            logger.debug("Không edit được message trạng thái", exc_info=True)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Trả lời /start bằng lời giới thiệu và danh sách lệnh."""
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Trả lời /help bằng hướng dẫn sử dụng tĩnh."""
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def cmd_status(message: Message, telegram_user_id: int, username: str | None) -> None:
    """Báo thống kê sử dụng LLM của chính user hỏi (đọc DB trong thread riêng)."""

        # Đọc DB lấy thống kê usage của user
    def _load_usage() -> dict:
        with session_scope() as session:
            user = get_or_create_user(session, telegram_user_id, username)
            return user_usage(session, user)

    usage = await asyncio.to_thread(_load_usage)
    await message.answer(
        "Thống kê sử dụng của bạn:\n"
        f"- Lời gọi LLM: {usage['llm_calls']}\n"
        f"- Prompt tokens: {usage['prompt_tokens']}\n"
        f"- Completion tokens: {usage['completion_tokens']}\n"
        f"- Tổng tokens: {usage['total_tokens']}\n"
        f"- Chi phí ước tính: ${usage['cost_usd']:.6f}"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, telegram_user_id: int) -> None:
    """Hủy lượt xử lý đang chạy của chính user; không có thì báo rõ."""
    # Tìm task đang chạy của user, hủy nếu có
    task = _running_tasks.get(telegram_user_id)
    if task is None or task.done():
        await message.answer("Hiện không có lượt xử lý nào đang chạy.")
        return
    task.cancel()
    await message.answer("Đã hủy lượt xử lý đang chạy.")


@router.message(F.document)
async def handle_document(message: Message, telegram_user_id: int) -> None:
    """Tải tài liệu về ``var/uploads/`` và xác nhận, chưa xử lý nội dung.

    Giới hạn 10MB để không kéo file lớn về máy; tóm tắt nội dung tài liệu là
    tính năng của sprint sau nên chỉ xác nhận đã nhận file.
    """
    document = message.document
    if document is None:  # filter F.document bảo đảm, giữ guard cho type checker
        return
    # Kiểm tra kích thước
    size = document.file_size or 0
    if size > MAX_DOCUMENT_BYTES:
        await message.answer(
            f"Tài liệu {size / (1024 * 1024):.1f}MB vượt giới hạn 10MB, mình không tải về được."
        )
        return

    # Tên file do người dùng đặt: chỉ giữ ký tự an toàn, tránh path traversal.
    # Làm sạch tên file, tải về thư mục uploads
    raw_name = document.file_name or "tai-lieu"
    safe_name = re.sub(r"[^\w.\-]+", "_", Path(raw_name).name) or "tai-lieu"
    dest = UPLOAD_DIR / f"{telegram_user_id}_{document.file_unique_id}_{safe_name}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await message.bot.download(document, destination=dest)

    await message.answer(
        f"Đã nhận tài liệu \"{raw_name}\" ({size} bytes) và lưu lại.\n"
        "Tính năng tóm tắt nội dung tài liệu sẽ có ở sprint sau."
    )


@router.message(F.text)
async def handle_text(message: Message, telegram_user_id: int, username: str | None) -> None:
    """Chạy một lượt chat: typing action, message trạng thái, worker thread.

    Luồng: gửi typing action và message "Đang xử lý...", chạy ``handle_message``
    trong thread với callback tiến độ thread-safe, rồi trả lời (cắt đoạn nếu
    dài hơn 4096 ký tự). Task được ghi vào registry để /cancel hủy được; hủy
    chỉ ngắt phía chờ, thread nền chạy nốt rồi bị bỏ kết quả — chấp nhận được
    cho phạm vi hiện tại.
    """
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        # Lệnh không được handler nào nhận: không đưa vào LLM (lệnh vốn miễn quota).
        await message.answer("Lệnh không được hỗ trợ. Gõ /help để xem hướng dẫn.")
        return
    # Từ chối nếu đang xử lý lượt khác
    if (running := _running_tasks.get(telegram_user_id)) is not None and not running.done():
        await message.answer(
            "Mình đang xử lý một yêu cầu khác của bạn. Chờ xong hoặc dùng /cancel nhé."
        )
        return

    # Gửi typing + tin trạng thái
    await message.bot.send_chat_action(message.chat.id, "typing")
    status = await message.answer(PROCESSING_TEXT)
    loop = asyncio.get_running_loop()
    editor = _StatusEditor(status, loop)

    # Tạo task chạy agent trong thread riêng
    task = asyncio.create_task(
        asyncio.to_thread(
            handle_message, telegram_user_id, username, text, editor.push_threadsafe
        )
    )
    _running_tasks[telegram_user_id] = task
    try:
    # Chờ kết quả, bắt lỗi hủy/crash
        reply = await task
    except asyncio.CancelledError:
        # /cancel hủy task này; handler bản thân không bị hủy nên chỉ báo lại.
        await editor.edit("Lượt xử lý đã bị hủy.")
        return
    except Exception:
        logger.exception("Xử lý tin nhắn của user %s thất bại", telegram_user_id)
        await editor.edit("Có lỗi khi xử lý yêu cầu. Bạn thử lại sau nhé.")
        return
    finally:
    # Xóa task khỏi registry khi xong
        if _running_tasks.get(telegram_user_id) is task:
            del _running_tasks[telegram_user_id]

    # Chia tin nhắn dài rồi gửi
    chunks = split_message(reply.text) or ["(Không có nội dung trả lời.)"]
    await editor.edit(chunks[0])
    for chunk in chunks[1:]:
        await message.answer(chunk)
