"""Helper truy vấn/ghi DB nhận `Session` làm tham số đầu, không tự commit."""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from laplace.models import Conversation, LLMCall, Message, Task, ToolCall, Trace, User


def get_or_create_user(session: Session, telegram_user_id: int, username: str | None) -> User:
    """Tìm user theo telegram_user_id, chưa có thì tạo; cập nhật username mới."""
    # Tìm theo telegram ID, chưa có thì tạo mới
    user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        user = User(telegram_user_id=telegram_user_id, username=username)
        session.add(user)
        session.flush()
    elif username is not None and user.username != username:
        user.username = username
    return user


def get_or_create_conversation(session: Session, user: User) -> Conversation:
    """Lấy conversation mới nhất của user, chưa có thì mở conversation mới."""
    # Lấy cuộc hội thoại mới nhất, chưa có thì mở mới
    conversation = session.scalar(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.id.desc())
        .limit(1)
    )
    if conversation is None:
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        session.flush()
    return conversation


def add_message(session: Session, conversation: Conversation, role: str, content: str) -> Message:
    """Ghi một tin nhắn vào conversation và trả về bản ghi đã flush."""
    # Thêm tin nhắn vào cuộc hội thoại
    message = Message(conversation_id=conversation.id, role=role, content=content)
    session.add(message)
    session.flush()
    return message


def recent_messages(
    session: Session, conversation: Conversation, limit: int = 10
) -> list[Message]:
    """Trả về tối đa `limit` tin nhắn gần nhất theo thứ tự thời gian tăng."""
    # Lấy N tin gần nhất, đảo lại cho đúng thứ tự thời gian
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def record_llm_call(
    session: Session,
    *,
    task_id: int | None = None,
    user_id: int | None = None,
    purpose: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    latency_ms: int,
) -> LLMCall:
    """Ghi log một lần gọi LLM; truyền `user_id` để usage tính theo người dùng."""
    # Ghi log 1 lần gọi LLM kèm token/cost
    call = LLMCall(
        task_id=task_id,
        user_id=user_id,
        purpose=purpose,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    session.add(call)
    session.flush()
    return call


def record_tool_call(
    session: Session,
    *,
    task_id: int | None = None,
    tool_name: str,
    params_json: str,
    result_json: str,
    ok: bool,
    error: str | None,
    latency_ms: int,
) -> ToolCall:
    """Ghi log một lần gọi tool với tham số/kết quả đã serialize sẵn."""
    # Ghi log 1 lần gọi tool kèm kết quả
    call = ToolCall(
        task_id=task_id,
        tool_name=tool_name,
        params_json=params_json,
        result_json=result_json,
        ok=ok,
        error=error,
        latency_ms=latency_ms,
    )
    session.add(call)
    session.flush()
    return call


def record_trace(
    session: Session,
    *,
    task_id: int | None = None,
    conversation_id: int | None = None,
    step_index: int,
    payload: dict,
) -> Trace:
    """Ghi snapshot trace; `payload` được serialize JSON (ensure_ascii=False)."""
    # Ghi snapshot 1 bước suy luận (JSON)
    trace = Trace(
        task_id=task_id,
        conversation_id=conversation_id,
        step_index=step_index,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    session.add(trace)
    session.flush()
    return trace


def user_usage(session: Session, user: User) -> dict[str, Any]:
    """Tổng hợp token/chi phí LLM của một user; nhóm rỗng trả toàn số 0.

    Tính các call gắn trực tiếp `user_id` hoặc gắn task thuộc user; call thỏa
    cả hai điều kiện chỉ được đếm một lần vì điều kiện OR trên cùng một dòng.
    """
    # Tổng hợp tất cả token/cost của 1 user từ DB
    task_ids = select(Task.id).where(Task.user_id == user.id)
    calls, prompt, completion, cost = session.execute(
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        ).where((LLMCall.user_id == user.id) | (LLMCall.task_id.in_(task_ids)))
    ).one()
    return {
        "llm_calls": int(calls),
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
        "cost_usd": round(float(cost), 6),
    }
