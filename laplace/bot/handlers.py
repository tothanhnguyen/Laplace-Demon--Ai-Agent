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
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from laplace.bot.textsplit import split_message
from laplace.db import session_scope
from laplace.repo import get_or_create_user, user_usage
from laplace.services.chat import _run_reserved_message
from laplace.services.memory import (
    forget_memory,
    load_memory,
    release_turn,
    request_cancel,
    try_reserve_turn,
)

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
    "/status — thống kê tokens và chi phí\n"
    "/memory — xem bộ nhớ phiên trong chat riêng\n"
    "/forget — xem phạm vi xóa; /forget confirm để xác nhận\n"
    "/cancel — yêu cầu dừng lượt đang xử lý"
)

HELP_TEXT = (
    "Cách dùng:\n"
    "- Nhắn văn bản bất kỳ để hỏi; mỗi người chỉ có một lượt chạy tại một thời điểm.\n"
    "- /status xem usage; /memory xem hội thoại/task gần nhất trong chat riêng.\n"
    "- /forget chỉ cảnh báo; /forget confirm xóa memory có liên kết nhưng giữ usage.\n"
    "- /cancel yêu cầu dừng ở checkpoint tiếp theo; lời gọi model/tool đang chạy không bị ngắt.\n"
    "- File tải lên chưa thuộc phạm vi /forget và chưa được tóm tắt ở Sprint 3."
)


class _StatusEditor:
    """Edit một message trạng thái duy nhất, nhận text từ thread khác an toàn.

    Callback tiến độ chạy trong worker thread nên không được đụng API aiogram
    trực tiếp; ``push_threadsafe`` chuyển text về event loop, chỉ giữ bản mới
    nhất và edit tuần tự để không dồn đống request khi tiến độ dồn dập.
    """

    def __init__(self, status: Message, loop: asyncio.AbstractEventLoop) -> None:
        self._status = status
        self._loop = loop
        self._latest: str | None = None
        self._draining = False

    def push_threadsafe(self, text: str) -> None:
        """Được gọi từ worker thread: đẩy text mới về event loop."""
        self._loop.call_soon_threadsafe(self._push, text)

    def _push(self, text: str) -> None:
        self._latest = text
        if not self._draining:
            self._draining = True
            self._loop.create_task(self._drain())

    async def _drain(self) -> None:
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
    """Yêu cầu worker của user dừng tại checkpoint an toàn kế tiếp."""
    if not request_cancel(telegram_user_id):
        await message.answer("Hiện không có lượt xử lý nào đang chạy.")
        return
    await message.answer(
        "Đã yêu cầu hủy. Lời gọi mô hình/công cụ đang chạy sẽ hoàn tất trước khi lượt dừng."
    )


def _is_private(message: Message) -> bool:
    return str(message.chat.type) in {"private", "ChatType.PRIVATE"}


def _memory_text(data: dict | None) -> str:
    if data is None:
        return "Bộ nhớ phiên đang trống."
    lines = [
        "Bộ nhớ phiên của bạn:",
        f"- Cuộc hội thoại: {data['conversations']}",
        f"- Tin nhắn: {data['messages']}",
        f"- Nhiệm vụ / lời gọi tool: {data['tasks']} / {data['tool_calls']}",
        f"- Lần hoạt động cuối: {data['last_activity'] or 'chưa có'}",
        f"- Usage giữ lại: {data['usage']['total_tokens']} tokens, "
        f"${data['usage']['cost_usd']:.6f}",
    ]
    if data.get("busy"):
        lines.append("- Trạng thái: đang xử lý; đây là dữ liệu đã commit gần nhất")
    if data.get("summary"):
        lines.append("- Tóm tắt: " + str(data["summary"])[:800])
    if data.get("recent_messages"):
        lines.append("- Hội thoại gần nhất:")
        lines.extend(
            f"  {item['role']}: {item['content']}" for item in data["recent_messages"]
        )
    if data.get("recent_tasks"):
        lines.append("- Nhiệm vụ gần nhất:")
        for item in data["recent_tasks"]:
            lines.append(f"  #{item['id']} [{item['status']}]: {item['goal']}")
            remaining = item.get("remaining")
            if remaining:
                lines.append(f"    còn lại: {'; '.join(remaining)}")
            for tool in item.get("tools", []):
                source = f" [{tool['source']}]" if tool.get("source") else ""
                outcome = "OK" if tool["ok"] else "ERROR"
                lines.append(
                    f"    tool {tool['name']}{source} ({outcome}): {tool['result']}"
                )
    return "\n".join(lines)


@router.message(Command("memory"))
async def cmd_memory(message: Message, telegram_user_id: int) -> None:
    """Xem memory của chính sender; không phát preview trong group."""
    if not _is_private(message):
        await message.answer("Hãy mở chat riêng với bot để xem bộ nhớ phiên.")
        return
    data = await asyncio.to_thread(load_memory, telegram_user_id)
    for chunk in split_message(_memory_text(data)):
        await message.answer(chunk)


@router.message(Command("forget"))
async def cmd_forget(
    message: Message, telegram_user_id: int, command: CommandObject
) -> None:
    """Cảnh báo hoặc xóa memory có liên kết sau xác nhận rõ ràng."""
    if not _is_private(message):
        await message.answer("Hãy mở chat riêng với bot để quản lý bộ nhớ phiên.")
        return
    if (command.args or "").strip() != "confirm":
        await message.answer(
            "Lệnh này xóa conversations, messages, tasks, steps, traces và tool results "
            "có liên kết của bạn. Usage, file đã tải lên, dữ liệu Telegram/provider và "
            "tool logs cũ không xác định owner vẫn được giữ.\n"
            "Gõ /forget confirm để xác nhận."
        )
        return
    status, result = await asyncio.to_thread(forget_memory, telegram_user_id)
    if status == "busy":
        await message.answer(
            "Lượt xử lý vẫn đang chạy. Hãy /cancel, chờ worker dừng rồi thử lại."
        )
        return
    if status == "empty":
        await message.answer("Bộ nhớ phiên đã trống; lịch sử usage vẫn được giữ.")
        return
    assert result is not None
    suffix = ""
    if result["legacy_unowned_tool_calls"]:
        suffix = (
            "\nMột số tool logs Sprint 2 không có owner nên không thể xóa an toàn "
            "theo từng người dùng."
        )
    await message.answer(
        "Đã xóa bộ nhớ phiên có liên kết của bạn; lịch sử usage và file tải lên "
        f"được giữ.{suffix}"
    )


@router.message(F.document)
async def handle_document(message: Message, telegram_user_id: int) -> None:
    """Tải tài liệu về ``var/uploads/`` và xác nhận, chưa xử lý nội dung.

    Giới hạn 10MB để không kéo file lớn về máy; tóm tắt nội dung tài liệu là
    tính năng của sprint sau nên chỉ xác nhận đã nhận file.
    """
    document = message.document
    if document is None:  # filter F.document bảo đảm, giữ guard cho type checker
        return
    size = document.file_size or 0
    if size > MAX_DOCUMENT_BYTES:
        await message.answer(
            f"Tài liệu {size / (1024 * 1024):.1f}MB vượt giới hạn 10MB, mình không tải về được."
        )
        return

    # Tên file do người dùng đặt: chỉ giữ ký tự an toàn, tránh path traversal.
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
    """Reserve trước await, chạy worker thread và chỉ worker thật release lease."""
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        await message.answer("Lệnh không được hỗ trợ. Gõ /help để xem hướng dẫn.")
        return

    lease = try_reserve_turn(telegram_user_id)
    if lease is None:
        await message.answer(
            "Mình đang xử lý một yêu cầu khác. Chờ xong hoặc dùng /cancel nhé."
        )
        return
    worker_started = False
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        status = await message.answer(PROCESSING_TEXT)
        loop = asyncio.get_running_loop()
        editor = _StatusEditor(status, loop)
        task = asyncio.create_task(
            asyncio.to_thread(
                _run_reserved_message,
                lease,
                telegram_user_id,
                username,
                text,
                editor.push_threadsafe,
            )
        )
        worker_started = True
        _running_tasks[telegram_user_id] = task

        def clear_finished(finished: asyncio.Task) -> None:
            if _running_tasks.get(telegram_user_id) is finished:
                del _running_tasks[telegram_user_id]

        task.add_done_callback(clear_finished)
        try:
            reply = await asyncio.shield(task)
        except asyncio.CancelledError:
            await editor.edit("Handler đã dừng chờ; worker vẫn hoàn tất an toàn.")
            raise
        except Exception:
            logger.exception("Xử lý tin nhắn của user %s thất bại", telegram_user_id)
            await editor.edit("Có lỗi khi xử lý yêu cầu. Bạn thử lại sau nhé.")
            return

        chunks = split_message(reply.text) or ["(Không có nội dung trả lời.)"]
        await editor.edit(chunks[0])
        for chunk in chunks[1:]:
            await message.answer(chunk)
    finally:
        if not worker_started:
            release_turn(lease)
