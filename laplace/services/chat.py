"""Xử lý một lượt chat: LLM thật + tool calling có kiểm tra dữ liệu (S2-07/08).

Đây là boundary sync dùng chung cho CLI và bot Telegram (bot gọi qua
``asyncio.to_thread``). Mỗi lượt: nạp hội thoại gần nhất từ DB, chạy vòng lặp
tối đa ``MAX_STEPS`` bước (LLM quyết định gọi tool hoặc trả lời), validate
mọi output LLM bằng Pydantic với self-correction, ghi ``llm_calls``,
``tool_calls``, ``traces`` và messages vào DB.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from laplace import repo
from laplace.db import session_scope
from laplace.llm.base import LLMProvider, LLMResult, get_provider
from laplace.prompts import (
    AgentAction,
    build_turn_messages,
    observation_message,
    validation_error_message,
)
from laplace.tools.base import execute, load_builtin_tools

MAX_STEPS = 5  # trần số bước tool trong một lượt, giữ từ Sprint 1
MAX_SCHEMA_RETRIES = 2  # số lần yêu cầu LLM tự sửa JSON sai schema
HISTORY_LIMIT = 10  # số message gần nhất nạp làm ngữ cảnh


@dataclass
class ChatReply:
    """Kết quả một lượt chat trả cho CLI/bot: text cuối và usage lượt này."""

    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class _TurnRecorder:
    """Gom token/cost các lời gọi LLM trong một lượt để báo cáo usage."""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0

    def add(self, result: LLMResult) -> None:
        """Cộng dồn số liệu từ một ``LLMResult``."""
        # Cộng dồn token/cost của 1 lần gọi LLM
        self.llm_calls += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.cost_usd += result.cost_usd

    def summary(self) -> dict[str, Any]:
        """Trả dict usage của lượt, cost làm tròn 6 chữ số."""
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


def _next_action(
    provider: LLMProvider,
    messages: list[dict[str, str]],
    on_llm_call: Callable[[LLMResult, str], None],
) -> AgentAction | None:
    """Hỏi LLM hành động kế tiếp, validate Pydantic với self-correction.

    Mỗi lần output sai schema, gửi thông điệp lỗi để mô hình tự sửa, tối đa
    ``MAX_SCHEMA_RETRIES`` lần; vẫn sai thì trả ``None`` để caller kết thúc
    lượt với lời giải thích trung thực. Mọi lời gọi (kể cả lần sửa) đều được
    báo qua ``on_llm_call`` để ghi ``llm_calls``.
    """
    # Lấy schema JSON để LLM trả đúng format
    schema = AgentAction.model_json_schema()
    for attempt in range(MAX_SCHEMA_RETRIES + 1):
        # Gọi LLM và ghi log
        result = provider.complete(messages, json_schema=schema)
        on_llm_call(result, "agent_action" if attempt == 0 else "schema_retry")
        error: str
        # LLM không trả JSON → báo lỗi
        if result.parsed is None:
            error = "output is not a JSON object"
        else:
            # Validate output, đúng thì trả về
            try:
                return AgentAction.model_validate(result.parsed)
            except ValidationError as e:
                error = str(e)
        # Sai schema → gửi lỗi cho LLM tự sửa
        if attempt == MAX_SCHEMA_RETRIES:
            return None
        messages.append({"role": "assistant", "content": result.content or "(empty)"})
        messages.append(validation_error_message(error))
    return None


def handle_message(
    telegram_user_id: int,
    username: str | None,
    text: str,
    progress: Callable[[str], None] | None = None,
) -> ChatReply:
    """Chạy trọn một lượt chat cho một người dùng Telegram.

    Input là identity Telegram và nội dung tin nhắn; output là ``ChatReply``
    với câu trả lời cuối và usage. Side effect: ghi user/conversation/messages,
    ``llm_calls`` (kèm user_id), ``tool_calls`` và ``traces`` vào DB; gọi mạng
    tới LLM provider theo cấu hình. ``progress`` (nếu có) nhận mô tả ngắn
    từng bước để bot cập nhật tiến độ.
    """
    # Nạp tool built-in (read_file...)
    load_builtin_tools()
    # Chọn provider theo .env
    provider = get_provider()
    recorder = _TurnRecorder()
    notify = progress or (lambda _msg: None)

    with session_scope() as session:
        # Tìm/tạo user và conversation trong DB
        user = repo.get_or_create_user(session, telegram_user_id, username)
        conversation = repo.get_or_create_conversation(session, user)
        # Lưu tin nhắn user
        repo.add_message(session, conversation, "user", text)
        # Lấy 10 tin nhắn gần nhất làm ngữ cảnh
        history = [
            {"role": m.role, "content": m.content}
            for m in repo.recent_messages(session, conversation, limit=HISTORY_LIMIT)
            if m.role in ("user", "assistant")
        ]
        # Message vừa lưu đã nằm cuối history; build_turn_messages sẽ thêm lại
        # yêu cầu hiện tại nên cắt nó khỏi phần hội thoại cũ.
        if history and history[-1] == {"role": "user", "content": text}:
            history = history[:-1]

        user_id = user.id
        conversation_id = conversation.id

        def on_llm_call(result: LLMResult, purpose: str) -> None:
            """Ghi một lời gọi LLM vào DB và cộng vào tổng của lượt."""
            recorder.add(result)
            repo.record_llm_call(
                session,
                user_id=user_id,
                purpose=purpose,
                provider=provider.name,
                model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
            )

        # Xây messages: system prompt + history + yêu cầu mới
        messages = build_turn_messages(text, history)
        reply_text = ""
        for step_index in range(MAX_STEPS + 1):
            notify(f"Bước {step_index + 1}: đang suy nghĩ...")
            # Hỏi LLM hành động tiếp theo
            action = _next_action(provider, messages, on_llm_call)
            # LLM trả sai liên tục → dừng, xin lỗi
            if action is None:
                reply_text = (
                    "Xin lỗi, mô hình trả về dữ liệu không hợp lệ nhiều lần "
                    "nên lượt này dừng lại. Bạn thử diễn đạt lại yêu cầu nhé."
                )
                repo.record_trace(
                    session,
                    conversation_id=conversation_id,
                    step_index=step_index,
                    payload={"event": "schema_failure", "request": text},
                )
                break
            # LLM muốn trả lời → lấy câu trả lời, dừng
            if action.action == "final":
                reply_text = action.final_answer or ""
                repo.record_trace(
                    session,
                    conversation_id=conversation_id,
                    step_index=step_index,
                    payload={"event": "final", "answer_preview": reply_text[:200]},
                )
                break
            # Chạm giới hạn bước → dừng, thông báo
            if step_index == MAX_STEPS:
                reply_text = (
                    f"Đã chạm giới hạn {MAX_STEPS} bước công cụ mà chưa xong; "
                    "lượt này dừng để tránh chạy vô hạn."
                )
                repo.record_trace(
                    session,
                    conversation_id=conversation_id,
                    step_index=step_index,
                    payload={"event": "step_limit", "request": text},
                )
                break

            # Chạy tool và ghi log
            notify(f"Bước {step_index + 1}: chạy tool {action.tool}...")
            result = execute(action.tool or "", action.params)
            payload = str(result.data) if result.ok else result.error
            repo.record_tool_call(
                session,
                tool_name=action.tool or "",
                params_json=json.dumps(action.params, ensure_ascii=False),
                result_json=json.dumps(
                    {"ok": result.ok, "payload": payload[:2000]}, ensure_ascii=False
                ),
                ok=result.ok,
                error="" if result.ok else result.error,
                latency_ms=0,
            )
            repo.record_trace(
                session,
                conversation_id=conversation_id,
                step_index=step_index,
                payload={
                    "event": "tool_call",
                    "tool": action.tool,
                    "params": action.params,
                    "ok": result.ok,
                },
            )
            # Đưa kết quả tool vào messages cho LLM xem
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(action.model_dump(), ensure_ascii=False),
                }
            )
            messages.append(observation_message(action.tool or "", result.ok, payload))

        # Lưu câu trả lời vào DB
        repo.add_message(session, conversation, "assistant", reply_text)

    return ChatReply(text=reply_text, usage=recorder.summary())
