"""Telegram commands, text turns, and bounded document turns."""

import asyncio
import logging
import re
import threading
from functools import partial
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from laplace.bot.textsplit import split_message
from laplace.config import Settings
from laplace.db import session_scope
from laplace.logging import safe_error_code
from laplace.repo import get_or_create_user, user_usage
from laplace.services import approvals, chat
from laplace.services.chat import REMINDER_DIRECTIVES, _run_reserved_message
from laplace.services.memory import (
    AttachmentBundle,
    TurnLease,
    accepts_chat_admission,
    account_guard,
    admit_chat_turn,
    forget_memory,
    load_memory,
    paused_token,
    release_turn,
    request_cancel,
    try_reserve_turn,
)

logger = logging.getLogger(__name__)

router = Router(name="laplace-bot")

PROCESSING_TEXT = "Đang xử lý..."
UPLOAD_DIR = Path("var/uploads")

# Shielded tasks own their real thread and publication even if a handler detaches.
_worker_tasks: set[asyncio.Task] = set()
_admitted_tasks: set[asyncio.Task] = set()

START_TEXT = (
    "Xin chào! Mình là Laplace's Demon — trợ lý chat chạy trên LLM.\n\n"
    "Bạn có thể hỏi thông tin web có nguồn hoặc đọc TXT/PDF/DOCX. "
    "Tạo/sửa/hủy reminder cần bạn duyệt bản xem trước trong chat riêng.\n"
    "Các lệnh hỗ trợ:\n"
    "/help — hướng dẫn và ví dụ\n"
    "/status — thống kê tokens và chi phí\n"
    "/memory — xem bộ nhớ và reminder ID trong chat riêng\n"
    "/forget — xem phạm vi xóa; /forget confirm để xác nhận\n"
    "/cancel — hủy đề xuất đang chờ hoặc dừng lượt agent đang xử lý"
)

HELP_TEXT = (
    "Cách dùng:\n"
    "- Web: hỏi ví dụ “Tìm thông tin mới nhất về Python 3.14 kèm nguồn”.\n"
    "- Tài liệu: trong chat riêng, gửi TXT/PDF/DOCX tối đa 10MB kèm yêu cầu; "
    "PDF scan không có text cần OCR nên chưa hỗ trợ.\n"
    "- Reminder: `/remind ...`, `/remind_update #ID ...`, `/remind_cancel #ID` "
    "chỉ đề xuất thao tác trong chat riêng, không phải sự đồng ý thực hiện. "
    "Hãy kiểm tra toàn bộ nội dung/lịch/đích nhận rồi bấm Xác nhận hoặc Hủy.\n"
    "- Đề xuất hết hạn sau 15 phút; khởi động lại bot làm đề xuất mất hiệu lực.\n"
    "- Tệp upload được giữ tạm khi chờ duyệt, rồi xóa khi lượt kết thúc/hủy/hết hạn.\n"
    "- /memory xem trạng thái chờ; /cancel hủy đề xuất hoặc dừng lượt chat/document, "
    "không hủy reminder đang gửi. /forget confirm xóa dữ liệu phiên.\n"
    "- Mỗi người chỉ có một thao tác tài khoản đang chạy hoặc một đề xuất chờ duyệt."
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
        self._drain_task: asyncio.Task | None = None
        self._closed = False

    def push_threadsafe(self, text: str) -> None:
        """Được gọi từ worker thread: đẩy text mới về event loop."""
        self._loop.call_soon_threadsafe(self._push, text)

    def _push(self, text: str) -> None:
        if self._closed:
            return
        self._latest = text
        if not self._draining:
            self._draining = True
            self._drain_task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self._latest is not None:
                text = self._latest
                self._latest = None
                await self.edit(text)
        finally:
            self._draining = False

    async def finish(self) -> None:
        """Drain progress I/O before final publication and eventual bot closure."""
        self._closed = True
        if self._drain_task is not None:
            await asyncio.shield(self._drain_task)

    async def edit(self, text: str) -> None:
        """Edit message trạng thái; lỗi edit (trùng nội dung...) chỉ ghi log."""
        try:
            await self._status.edit_text(text)
        except Exception as exc:
            logger.debug("Không edit được message trạng thái: %s", safe_error_code(exc))


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
        f"- Lời gọi LLM logic / attempts: {usage['llm_calls']} / {usage['physical_attempts']}\n"
        f"- Prompt tokens: {usage['prompt_tokens']}\n"
        f"- Completion tokens: {usage['completion_tokens']}\n"
        f"- Tổng tokens: {usage['total_tokens']}\n"
        f"- Chi phí đã biết: ${usage['known_cost_usd']:.6f}\n"
        f"- Ngân sách đã tính (gồm dự phòng chưa biết): ${usage['budget_charge_usd']:.6f}\n"
        f"- Attempts chưa biết usage: {usage['unknown_attempts']}"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, telegram_user_id: int) -> None:
    """Cancel both an active worker and a pending generation, never a thread."""
    running = request_cancel(telegram_user_id)
    pending = chat.cancel_paused_turn(telegram_user_id)
    if pending is not None:
        await remove_approval_keyboard(message.bot, pending)
        await message.answer("Đã hủy đề xuất đang chờ duyệt.")
    elif running:
        await message.answer(
            "Đã yêu cầu hủy. Lời gọi mô hình/công cụ đang chạy sẽ hoàn tất trước khi lượt dừng."
        )
    else:
        await message.answer("Hiện không có lượt xử lý nào đang chạy hoặc chờ duyệt.")


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
        f"- Reminder tổng/đang hoạt động: "
        f"{data.get('reminders', {}).get('total', 0)} / "
        f"{data.get('reminders', {}).get('active', 0)}",
        f"- Lần hoạt động cuối: {data['last_activity'] or 'chưa có'}",
        f"- Usage giữ lại: {data['usage']['total_tokens']} tokens; "
        f"logic / attempts: {data['usage']['llm_calls']} / {data['usage']['physical_attempts']}",
        f"- Chi phí đã biết: ${data['usage']['known_cost_usd']:.6f}; "
        f"ngân sách đã tính: ${data['usage']['budget_charge_usd']:.6f}; "
        f"attempts chưa biết usage: {data['usage']['unknown_attempts']}",
    ]
    if data.get("busy"):
        lines.append("- Trạng thái: đang xử lý; đây là dữ liệu đã commit gần nhất")
    if data.get("summary"):
        lines.append("- Tóm tắt: " + str(data["summary"])[:800])
    if data.get("recent_messages"):
        lines.append("- Hội thoại gần nhất:")
        lines.extend(f"  {item['role']}: {item['content']}" for item in data["recent_messages"])
    if data.get("recent_tasks"):
        lines.append("- Nhiệm vụ gần nhất:")
        for item in data["recent_tasks"]:
            lines.append(f"  #{item['id']} [{item['status']}]: {item['goal']}")
            remaining = item.get("remaining")
            if remaining:
                lines.append(f"    còn lại: {'; '.join(remaining)}")
            for tool in item.get("tools", []):
                sources = tool.get("sources") or []
                source_text = f" [{'; '.join(sources)}]" if sources else ""
                outcome = "OK" if tool["ok"] else "ERROR"
                lines.append(f"    tool {tool['name']}{source_text} ({outcome}): {tool['result']}")
    if data.get("recent_reminders"):
        lines.append("- Reminder gần nhất:")
        lines.extend(
            f"  #{item['id']} [{item['status']}] {item['run_at']} "
            f"({item.get('timezone', 'UTC')}): {item['message']}"
            for item in data["recent_reminders"]
        )
    return "\n".join(lines)


@router.message(Command("memory"))
async def cmd_memory(message: Message, telegram_user_id: int) -> None:
    """Xem memory của chính sender; không phát preview trong group."""
    if not _is_private(message):
        await message.answer("Hãy mở chat riêng với bot để xem bộ nhớ phiên.")
        return
    data = await asyncio.to_thread(load_memory, telegram_user_id)
    token = paused_token(telegram_user_id)
    if token is not None:
        record = approvals.get_approval(token, telegram_user_id=telegram_user_id)
        if record is not None:
            waiting = (
                f"\n- Đang chờ bạn duyệt {record.tool_name} cho nhiệm vụ #{record.task_id}. "
                "Xem đầy đủ bản xem trước, chọn Xác nhận/Hủy hoặc dùng /cancel. "
                f"Còn tối đa {max(0, record.expires_at - approvals.epoch_now())} giây; "
                "khởi động lại bot làm đề xuất mất hiệu lực. "
                "Tệp upload được giữ tạm cho đến khi lượt kết thúc/hủy/hết hạn."
            )
            if data is None:
                await message.answer("Đang chờ duyệt." + waiting)
                return
        else:
            waiting = ""
    else:
        waiting = ""
    for chunk in split_message(_memory_text(data) + waiting):
        await message.answer(chunk)


@router.message(Command("forget"))
async def cmd_forget(message: Message, telegram_user_id: int, command: CommandObject) -> None:
    """Cảnh báo hoặc xóa memory có liên kết sau xác nhận rõ ràng."""
    if not _is_private(message):
        await message.answer("Hãy mở chat riêng với bot để quản lý bộ nhớ phiên.")
        return
    if (command.args or "").strip() != "confirm":
        await message.answer(
            "Lệnh này xóa conversations, messages, tasks, tool results, đề xuất chờ duyệt "
            "và reminders có liên kết của bạn. Usage và dữ liệu Telegram/provider vẫn được giữ. "
            "Tệp upload đang chờ duyệt chỉ được dọn sau khi xóa phiên thành công.\n"
            "Gõ /forget confirm để xác nhận."
        )
        return
    token = paused_token(telegram_user_id)
    pending = approvals.get_approval(token, telegram_user_id=telegram_user_id) if token else None
    status, result = await asyncio.to_thread(forget_memory, telegram_user_id)
    if status == "busy":
        await message.answer(
            "Đang có thao tác tài khoản (chat, tài liệu hoặc reminder). "
            "Chờ hoàn tất rồi dùng /forget confirm lại."
        )
        return
    if status == "empty":
        await message.answer("Bộ nhớ phiên đã trống; lịch sử usage vẫn được giữ.")
        return
    if status == "delivery_busy":
        await message.answer(
            "Một reminder đang được gửi. Chờ lần gửi hoàn tất rồi dùng /forget confirm lại."
        )
        return
    assert result is not None
    if pending is not None:
        await remove_approval_keyboard(message.bot, pending)
    suffix = ""
    if result["legacy_unowned_tool_calls"]:
        suffix = (
            "\nMột số tool logs Sprint 2 không có owner nên không thể xóa an toàn "
            "theo từng người dùng."
        )
    if not result.get("cleanup_complete", True):
        suffix += (
            "\nĐã thu hồi quyền duyệt nhưng chưa dọn hết tệp tạm; "
            "bot sẽ thử dọn khi khởi động."
        )
    await message.answer(
        "Đã xóa bộ nhớ phiên, đề xuất chờ duyệt và reminder có liên kết của bạn; usage được giữ."
        f"{suffix}"
    )


@router.message(F.document)
async def handle_document(
    message: Message, telegram_user_id: int, username: str | None = None
) -> None:
    """Reserve before download; each managed upload has one ownership bundle."""
    document = message.document
    if document is None:
        return
    limit = Settings().document_max_bytes
    size = document.file_size or 0
    if size > limit:
        await message.answer(
            f"Tài liệu {size / (1024 * 1024):.1f}MB vượt giới hạn 10MB, mình không tải về được."
        )
        return
    if not _is_private(message):
        await message.answer("Hãy mở chat riêng với bot để xử lý tài liệu.")
        return
    lease = admit_chat_turn(telegram_user_id)
    if lease is None:
        await _answer_admission_denied(message, telegram_user_id)
        return

    safe_name = re.sub(
        r"[^\w.\-]+", "_", Path(document.file_name or "tai-lieu").name
    )[:128] or "tai-lieu"
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", document.file_unique_id)[:128] or "file"
    attachment_id = f"telegram-{safe_id}"
    dest = UPLOAD_DIR / f"laplace-{uuid4().hex}-{safe_name}"
    bundle = AttachmentBundle({attachment_id: dest}, managed_root=UPLOAD_DIR)
    handed_off = False
    admission = asyncio.current_task()
    if admission is not None:
        _admitted_tasks.add(admission)
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await message.bot.download(document, destination=dest)
        if dest.stat().st_size > limit:
            await message.answer("Tài liệu tải về vượt giới hạn 10MB.")
            return
        prompt = (message.caption or "").strip() or "Hãy tóm tắt tài liệu đính kèm."
        handed_off = True
        await _run_telegram_turn(
            message,
            telegram_user_id,
            username,
            prompt,
            attachments={attachment_id: dest},
            lease=lease,
            attachment_bundle=bundle,
        )
    finally:
        if not handed_off:
            release_turn(lease)
            bundle.cleanup_worker()
        if admission is not None:
            _admitted_tasks.discard(admission)


@router.message(F.text)
async def handle_text(message: Message, telegram_user_id: int, username: str | None) -> None:
    """Run a normal text turn through the shared Telegram turn helper."""
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        directive = text.split(maxsplit=1)[0].casefold()
        if directive not in REMINDER_DIRECTIVES:
            await message.answer("Lệnh không được hỗ trợ. Gõ /help để xem hướng dẫn.")
            return
    await _run_telegram_turn(message, telegram_user_id, username, text)


async def _answer_admission_denied(message: Message, telegram_user_id: int) -> None:
    if paused_token(telegram_user_id) is not None:
        await message.answer(
            "Bạn đang có đề xuất chờ duyệt. Dùng các nút trong bản xem trước, "
            "/memory, /cancel hoặc /forget confirm trước khi gửi yêu cầu mới."
        )
    else:
        await message.answer("Mình đang xử lý một yêu cầu khác. Chờ xong hoặc dùng /cancel nhé.")


async def remove_approval_keyboard(bot, record) -> None:
    """Best-effort UI cleanup after the authoritative DB transition."""
    if record.preview_message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=record.chat_id,
            message_id=record.preview_message_id,
            reply_markup=None,
        )
    except Exception as exc:
        logger.debug("Không gỡ được nút duyệt: %s", safe_error_code(exc))


async def _publish_approval(message: Message, pause, detached) -> None:
    """Send material facts first, bind the real message, then expose decisions."""
    preview = None
    try:
        if (
            not _is_private(message)
            or message.chat.id != pause.chat_id
            or pause.chat_id != pause.telegram_user_id
        ):
            raise RuntimeError("Approval preview requires its private destination")
        preview = await message.answer(pause.preview, parse_mode=None, reply_markup=None)
        if detached.is_set():
            chat.fail_approval_publication(pause.token)
            return
        with account_guard():
            bound = approvals.bind_preview(
                pause.token,
                telegram_user_id=pause.telegram_user_id,
                chat_id=message.chat.id,
                message_id=preview.message_id,
            )
            if (
                detached.is_set()
                or not accepts_chat_admission()
                or not bound
                or not chat.has_checkpoint(pause.token, pause.telegram_user_id)
            ):
                raise RuntimeError("Approval is no longer publishable")
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Xác nhận", callback_data=f"ap:a:{pause.token}"),
                    InlineKeyboardButton(text="Hủy", callback_data=f"ap:r:{pause.token}"),
                ]
            ]
        )
        await preview.edit_reply_markup(reply_markup=keyboard)
        # A cancel/forget/expiry can win while Telegram is applying the edit.
        # Reconcile afterward so a late edit cannot leave terminal buttons behind.
        with account_guard():
            current = approvals.get_approval(
                pause.token, telegram_user_id=pause.telegram_user_id
            )
            publishable = bool(
                not detached.is_set()
                and accepts_chat_admission()
                and chat.has_checkpoint(pause.token, pause.telegram_user_id)
                and current is not None
                and current.status == "pending"
                and current.expires_at > approvals.epoch_now()
                and current.preview_message_id == preview.message_id
            )
        if not publishable:
            chat.fail_approval_publication(pause.token)
            await preview.edit_reply_markup(reply_markup=None)
    except BaseException as exc:
        chat.fail_approval_publication(pause.token)
        if preview is not None:
            try:
                await preview.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        if not isinstance(exc, Exception):
            raise
        if not detached.is_set():
            await message.answer(
                "Không thể hoàn tất công bố đề xuất. Dùng /memory để kiểm tra trạng thái; "
                "nếu đề xuất đã mất hiệu lực, hãy gửi yêu cầu mới."
            )


async def _deliver_reply(message: Message, editor: _StatusEditor, reply, detached) -> None:
    if reply.pause is not None:
        if not detached.is_set():
            for chunk in split_message(reply.text):
                await message.answer(chunk, parse_mode=None)
            await editor.edit("Đang chờ bạn duyệt bản xem trước bên dưới.")
        if detached.is_set():
            chat.fail_approval_publication(reply.pause.token)
        else:
            await _publish_approval(message, reply.pause, detached)
        return
    chunks = split_message(reply.text) or ["(Không có nội dung trả lời.)"]
    await editor.edit(chunks[0])
    for chunk in chunks[1:]:
        await message.answer(chunk, parse_mode=None)


def _release_unstarted_worker(lease: TurnLease, approval_token: str | None) -> None:
    """Revoke an approved claim when no real thread took ownership of its lease."""
    if lease._released:
        return
    try:
        if approval_token is not None:
            with account_guard():
                terminal = approvals.terminate_approval(
                    approval_token,
                    status="interrupted",
                    telegram_user_id=lease.user_id,
                    allowed_statuses=("approved",),
                )
                if terminal is not None:
                    chat.discard_checkpoint(approval_token)
    finally:
        release_turn(lease)


def _launch_worker(
    telegram_user_id: int,
    message: Message,
    editor: _StatusEditor,
    run,
    bundle,
    detached,
    *,
    lease: TurnLease,
    approval_token: str | None = None,
):
    """The tracked task owns the thread, reply publication and eventual cleanup."""
    entered = threading.Event()

    def enter_worker():
        entered.set()
        return run()

    async def work() -> None:
        reply = None
        try:
            thread = asyncio.create_task(asyncio.to_thread(enter_worker))
            while True:
                try:
                    reply = await asyncio.shield(thread)
                    break
                except asyncio.CancelledError:
                    detached.set()
                    request_cancel(telegram_user_id)
                    if thread.done():
                        # A worker-side cancellation is already a real completion.
                        reply = thread.result()
                        break
                    # Repeated task cancellation still cannot abandon a real thread.
            await editor.finish()
            if detached.is_set():
                if reply.pause is not None:
                    chat.fail_approval_publication(reply.pause.token)
            else:
                await _deliver_reply(message, editor, reply, detached)
        except BaseException as exc:
            if reply is not None and reply.pause is not None:
                chat.fail_approval_publication(reply.pause.token)
            if not entered.is_set():
                _release_unstarted_worker(lease, approval_token)
            if not isinstance(exc, Exception):
                raise
            logger.error("Xử lý lượt thất bại: %s", safe_error_code(exc))
            if not detached.is_set():
                await editor.edit("Có lỗi khi xử lý yêu cầu. Bạn thử lại sau nhé.")
        finally:
            try:
                await editor.finish()
            finally:
                if bundle is not None:
                    bundle.cleanup_worker()

    coroutine = work()
    try:
        task = asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise
    _worker_tasks.add(task)

    def clear_finished(finished: asyncio.Task) -> None:
        _worker_tasks.discard(finished)
        if not entered.is_set():
            try:
                _release_unstarted_worker(lease, approval_token)
            except Exception as exc:
                logger.error("Không dọn được worker chưa chạy: %s", safe_error_code(exc))
            finally:
                if bundle is not None:
                    bundle.cleanup_worker()
        # Retrieve detached failures without logging private exception payloads.
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(clear_finished)
    return task


async def drain_chat_workers() -> None:
    """Drain admitted I/O and real threads before any transport session is closed."""
    while True:
        active = (_admitted_tasks | _worker_tasks) - {asyncio.current_task()}
        if not active:
            return
        await asyncio.gather(
            *(asyncio.shield(task) for task in active), return_exceptions=True
        )


async def _run_telegram_turn(
    message: Message,
    telegram_user_id: int,
    username: str | None,
    text: str,
    *,
    attachments: dict[str, Path] | None = None,
    lease: TurnLease | None = None,
    attachment_bundle: AttachmentBundle | None = None,
) -> None:
    """Use an already reserved document lease, or atomically admit a text turn."""
    bundle = attachment_bundle or AttachmentBundle(attachments or {})
    if lease is None:
        lease = admit_chat_turn(telegram_user_id)
    if lease is None:
        bundle.cleanup_worker()
        await _answer_admission_denied(message, telegram_user_id)
        return
    worker_started = False
    detached = asyncio.Event()
    admission = asyncio.current_task()
    if admission is not None:
        _admitted_tasks.add(admission)
    try:
        if not accepts_chat_admission():
            return
        await message.bot.send_chat_action(message.chat.id, "typing")
        status = await message.answer(PROCESSING_TEXT)
        editor = _StatusEditor(status, asyncio.get_running_loop())
        with account_guard():
            if not accepts_chat_admission():
                return
            task = _launch_worker(
                telegram_user_id,
                message,
                editor,
                partial(
                    _run_reserved_message,
                    lease,
                    telegram_user_id,
                    username,
                    text,
                    editor.push_threadsafe,
                    transport="telegram",
                    is_private_chat=_is_private(message),
                    attachments=attachments,
                    chat_id=message.chat.id,
                    attachment_bundle=bundle,
                ),
                bundle,
                detached,
                lease=lease,
            )
            worker_started = True
        await asyncio.shield(task)
    except asyncio.CancelledError:
        detached.set()
        raise
    finally:
        if not worker_started:
            release_turn(lease)
            bundle.cleanup_worker()
        if admission is not None:
            _admitted_tasks.discard(admission)


@router.callback_query(F.data.startswith("ap:"))
async def handle_approval_callback(callback: CallbackQuery) -> None:
    """Only a private owner click on the bound preview can claim a generation."""
    answered = False
    lease = None
    claimed = None
    worker_started = False
    detached = asyncio.Event()
    admission = asyncio.current_task()

    async def answer(text: str) -> None:
        nonlocal answered
        answered = True
        try:
            await callback.answer(text)
        except Exception as exc:
            logger.debug("Không trả lời được callback: %s", safe_error_code(exc))

    try:
        parts = (callback.data or "").split(":")
        message = callback.message
        if (
            len(parts) != 3
            or parts[0] != "ap"
            or parts[1] not in {"a", "r"}
            or not parts[2]
            or len((callback.data or "").encode("utf-8")) > 64
            or callback.inline_message_id is not None
            or not isinstance(message, Message)
            or not _is_private(message)
        ):
            await answer("Nút duyệt không hợp lệ. Hãy mở chat riêng với bot.")
            return
        actor = callback.from_user.id
        token = parts[2]
        with account_guard():
            if not chat.has_checkpoint(token, actor):
                stale = True
            else:
                stale = False
        if stale:
            await answer("Đề xuất đã hết hiệu lực hoặc không thuộc bạn.")
            return
        if parts[1] == "r":
            with account_guard():
                rejected = approvals.reject_approval(
                    token,
                    telegram_user_id=actor,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                if rejected is not None:
                    chat.discard_checkpoint(token)
            if rejected is None:
                await answer("Đề xuất đã hết hiệu lực hoặc không thuộc bạn.")
                return
            await answer("Đã hủy đề xuất; không thực hiện thao tác.")
            await remove_approval_keyboard(message.bot, rejected)
            return
        with account_guard():
            if not accepts_chat_admission():
                lease = None
                stopping = True
            else:
                stopping = False
                lease = try_reserve_turn(actor, kind="chat")
                if lease is not None and chat.has_checkpoint(token, actor):
                    claimed = approvals.claim_approval(
                        token,
                        telegram_user_id=actor,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                    )
        if stopping:
            await answer("Bot đang dừng; đề xuất chưa được thực hiện.")
            return
        if lease is None:
            await answer("Đang có thao tác tài khoản khác. Hãy bấm lại khi hoàn tất.")
            return
        if admission is not None:
            _admitted_tasks.add(admission)
        if claimed is None:
            await answer("Đề xuất đã hết hiệu lực hoặc không thuộc bạn.")
            return
        await answer("Đã nhận xác nhận; đang xử lý.")
        await remove_approval_keyboard(message.bot, claimed)
        status = await message.answer(PROCESSING_TEXT)
        editor = _StatusEditor(status, asyncio.get_running_loop())
        with account_guard():
            if not accepts_chat_admission():
                raise RuntimeError("Bot stopped before approval worker launch")
            task = _launch_worker(
                actor,
                message,
                editor,
                partial(chat.resume_approved, lease, token, progress=editor.push_threadsafe),
                None,
                detached,
                lease=lease,
                approval_token=token,
            )
            worker_started = True
        await asyncio.shield(task)
    except asyncio.CancelledError:
        detached.set()
        raise
    except Exception as exc:
        logger.error("Callback duyệt thất bại: %s", safe_error_code(exc))
        if not answered:
            await answer("Không thể xử lý đề xuất; thao tác chưa được thực hiện.")
        elif claimed is not None and not worker_started:
            try:
                await message.answer(
                    "Không thể khởi chạy thao tác đã duyệt; "
                    "đề xuất bị ngắt và chưa được thực hiện. "
                    "Hãy gửi yêu cầu mới."
                )
            except Exception:
                pass
    finally:
        try:
            if claimed is not None and not worker_started:
                with account_guard():
                    terminal = approvals.terminate_approval(
                        claimed.token,
                        status="interrupted",
                        telegram_user_id=claimed.telegram_user_id,
                        allowed_statuses=("approved",),
                    )
                    if terminal is not None:
                        chat.discard_checkpoint(claimed.token)
        finally:
            if lease is not None and not worker_started:
                release_turn(lease)
            if admission is not None:
                _admitted_tasks.discard(admission)
            if not answered:
                await answer("Đề xuất chưa được thực hiện; hãy thử lại hoặc gửi yêu cầu mới.")
