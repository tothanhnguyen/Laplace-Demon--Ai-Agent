"""Helper truy vấn/ghi DB nhận Session làm tham số đầu, không tự commit."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from laplace.models import (
    Conversation,
    LLMCall,
    Message,
    Step,
    Task,
    ToolCall,
    Trace,
    User,
    utcnow,
)

DB_EXCERPT_CHARS = 1_200


def find_user(session: Session, telegram_user_id: int) -> User | None:
    """Tìm user nhưng không tạo row mới."""
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def get_or_create_user(session: Session, telegram_user_id: int, username: str | None) -> User:
    """Tìm user theo Telegram ID, chưa có thì tạo và cập nhật username mới."""
    user = find_user(session, telegram_user_id)
    if user is None:
        user = User(telegram_user_id=telegram_user_id, username=username)
        session.add(user)
        session.flush()
    elif username is not None and user.username != username:
        user.username = username
    return user


def get_or_create_conversation(session: Session, user: User) -> Conversation:
    """Lấy conversation mới nhất của user, chưa có thì tạo."""
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
    """Ghi message và trả row đã flush để caller dùng ID chính xác."""
    message = Message(conversation_id=conversation.id, role=role, content=content)
    session.add(message)
    session.flush()
    return message


def recent_messages(
    session: Session, conversation: Conversation, limit: int = 10
) -> list[Message]:
    """Trả tối đa limit message mới nhất theo thứ tự tăng dần."""
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def conversation_messages(
    session: Session,
    conversation_id: int,
    *,
    after_id: int | None = None,
    before_id: int | None = None,
) -> list[Message]:
    """Đọc history của một conversation theo khoảng ID, không đoán bằng nội dung."""
    query = select(Message).where(Message.conversation_id == conversation_id)
    if after_id is not None:
        query = query.where(Message.id > after_id)
    if before_id is not None:
        query = query.where(Message.id < before_id)
    return list(session.scalars(query.order_by(Message.id)).all())


def conversation_message_char_count(
    session: Session,
    conversation_id: int,
    *,
    after_id: int | None = None,
    before_id: int | None = None,
) -> int:
    """Count a message range in SQL without materializing its content."""
    query = select(func.coalesce(func.sum(func.length(Message.content)), 0)).where(
        Message.conversation_id == conversation_id
    )
    if after_id is not None:
        query = query.where(Message.id > after_id)
    if before_id is not None:
        query = query.where(Message.id < before_id)
    return int(session.scalar(query) or 0)


def _bounded_db_excerpt(
    prefix: str | None,
    suffix: str | None,
    total_chars: int,
    limit: int = DB_EXCERPT_CHARS,
) -> str:
    """Reconstruct a bounded head/tail excerpt selected by SQL."""
    prefix = prefix or ""
    if total_chars <= limit:
        return prefix
    omitted = total_chars - limit
    while True:
        marker = f"\n…[đã lược {omitted} ký tự]…\n"
        room = max(0, limit - len(marker))
        corrected = total_chars - room
        if corrected == omitted:
            break
        omitted = corrected
    head_chars = room // 2
    tail_chars = room - head_chars
    return prefix[:head_chars] + marker + (suffix or "")[-tail_chars:]


def conversation_message_page(
    session: Session,
    conversation_id: int,
    *,
    after_id: int | None,
    before_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Load one oldest-first page with bounded content projections."""
    query = select(
        Message.id,
        Message.role,
        func.length(Message.content),
        func.substr(Message.content, 1, DB_EXCERPT_CHARS),
        func.substr(Message.content, -DB_EXCERPT_CHARS),
    ).where(
        Message.conversation_id == conversation_id,
        Message.id < before_id,
    )
    if after_id is not None:
        query = query.where(Message.id > after_id)
    rows = session.execute(query.order_by(Message.id).limit(limit)).all()
    return [
        {
            "id": row_id,
            "role": role,
            "content": _bounded_db_excerpt(prefix, suffix, int(total_chars or 0)),
        }
        for row_id, role, total_chars, prefix, suffix in rows
    ]


def recent_conversation_messages(
    session: Session,
    conversation_id: int,
    *,
    before_id: int,
    after_id: int | None = None,
    limit: int,
) -> list[Message]:
    """Load a bounded recent tail in chronological order."""
    query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.id < before_id,
    )
    if after_id is not None:
        query = query.where(Message.id > after_id)
    rows = session.scalars(query.order_by(Message.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def create_task(
    session: Session,
    *,
    user_id: int,
    conversation_id: int,
    request_message_id: int,
    goal: str,
) -> Task:
    """Tạo task ngay trước tool đầu tiên."""
    task = Task(
        user_id=user_id,
        conversation_id=conversation_id,
        request_message_id=request_message_id,
        goal=goal,
        status="running",
    )
    session.add(task)
    session.flush()
    return task


def append_step(
    session: Session,
    *,
    task_id: int,
    step_index: int,
    name: str,
    status: str,
    detail: dict[str, Any],
) -> Step:
    """Ghi snapshot v1 của một outer decision."""
    step = Step(
        task_id=task_id,
        step_index=step_index,
        name=name,
        status=status,
        detail=json.dumps({"version": 1, **detail}, ensure_ascii=False),
    )
    session.add(step)
    session.flush()
    return step


def close_task(session: Session, task_id: int, status: str) -> None:
    """Đóng task nếu nó còn running; terminal đầu tiên thắng."""
    session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "running")
        .values(status=status, updated_at=utcnow())
    )

def task_snapshot(session: Session, task_id: int) -> dict[str, Any]:
    """Tạo plain snapshot cho context/progress từ dữ liệu đã ghi."""
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(f"Không tìm thấy task #{task_id}")
    steps = list(
        session.scalars(select(Step).where(Step.task_id == task_id).order_by(Step.id)).all()
    )
    criteria: list[str] | None = None
    remaining: list[str] | None = None
    for step in steps:
        if not step.detail:
            continue
        try:
            detail = json.loads(step.detail)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(detail, dict):
            continue
        state = detail.get("task_state") or {}
        if not isinstance(state, dict):
            continue
        criteria_value = state.get("acceptance_criteria")
        remaining_value = state.get("remaining_work")
        if isinstance(criteria_value, list) and all(
            isinstance(item, str) for item in criteria_value
        ):
            criteria = criteria_value
        if isinstance(remaining_value, list) and all(
            isinstance(item, str) for item in remaining_value
        ):
            remaining = remaining_value
    tool_rows = session.execute(
        select(
            ToolCall.tool_name,
            func.count(ToolCall.id),
            func.sum(case((ToolCall.ok.is_(False), 1), else_=0)),
        )
        .where(ToolCall.task_id == task_id)
        .group_by(ToolCall.tool_name)
        .order_by(ToolCall.tool_name)
    ).all()
    tool_count = int(
        session.scalar(select(func.count(ToolCall.id)).where(ToolCall.task_id == task_id)) or 0
    )
    return {
        "id": task.id,
        "goal": task.goal,
        "status": task.status,
        "decision_index": steps[-1].step_index if steps else 0,
        "tool_count": tool_count,
        "tools": [
            {"name": name, "count": int(count), "failed": int(failed or 0)}
            for name, count, failed in tool_rows
        ],
        "criteria": criteria,
        "remaining": remaining,
    }
def tool_rows_for_request_ids(
    session: Session, request_message_ids: list[int]
) -> list[dict[str, Any]]:
    """Read bounded tool projections without materializing full result payloads."""
    if not request_message_ids:
        return []
    rows = session.execute(
        select(
            ToolCall.id,
            Task.request_message_id,
            ToolCall.tool_name,
            ToolCall.ok,
            ToolCall.params_json,
            func.length(ToolCall.result_json),
            func.substr(ToolCall.result_json, 1, DB_EXCERPT_CHARS),
            func.substr(ToolCall.result_json, -DB_EXCERPT_CHARS),
        )
        .join(Task, ToolCall.task_id == Task.id)
        .where(Task.request_message_id.in_(request_message_ids))
        .order_by(ToolCall.id)
    ).all()
    result: list[dict[str, Any]] = []
    for (
        call_id,
        request_id,
        tool_name,
        ok,
        params_json,
        result_chars,
        result_prefix,
        result_suffix,
    ) in rows:
        try:
            params = json.loads(params_json)
        except (TypeError, json.JSONDecodeError):
            params = {}
        if not isinstance(params, dict):
            params = {}
        total_chars = int(result_chars or 0)
        stored_text = _bounded_db_excerpt(
            result_prefix,
            result_suffix,
            total_chars,
        )
        stored: dict[str, Any] = {}
        if total_chars <= DB_EXCERPT_CHARS:
            try:
                decoded = json.loads(stored_text)
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            if isinstance(decoded, dict):
                stored = decoded
        result.append(
            {
                "id": call_id,
                "request_message_id": request_id,
                "tool_name": tool_name,
                "ok": ok,
                "source": params.get("path"),
                "payload": (
                    stored.get("payload", "")
                    if total_chars <= DB_EXCERPT_CHARS
                    else stored_text
                ),
            }
        )
    return result




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
    """Ghi usage LLM; user_id trực tiếp bảo toàn accounting khi task bị xóa."""
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
    """Ghi full payload do tool trả về; task_id quy thuộc row cho user."""
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
    """Ghi trace JSON theo task/conversation."""
    trace = Trace(
        task_id=task_id,
        conversation_id=conversation_id,
        step_index=step_index,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    session.add(trace)
    session.flush()
    return trace


def update_conversation_summary(
    session: Session,
    *,
    conversation_id: int,
    old_watermark: int | None,
    summary: str,
    new_watermark: int,
) -> bool:
    """Compare-and-update summary và watermark trong cùng statement."""
    condition = Conversation.summary_until_message_id.is_(None)
    if old_watermark is not None:
        condition = Conversation.summary_until_message_id == old_watermark
    result = session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, condition)
        .values(summary=summary, summary_until_message_id=new_watermark)
    )
    return result.rowcount == 1


def user_usage(session: Session, user: User) -> dict[str, Any]:
    """Tổng hợp usage theo direct user hoặc task ownership, không đếm đôi."""
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


def session_stats(session: Session, user: User) -> dict[str, Any]:
    """Đọc thống kê và preview hữu hạn của memory thuộc một user."""
    conversation_ids = select(Conversation.id).where(Conversation.user_id == user.id)
    task_ids = select(Task.id).where(Task.user_id == user.id)
    latest = session.scalar(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.id.desc())
        .limit(1)
    )
    recent = [] if latest is None else recent_messages(session, latest, limit=4)
    recent_tasks = list(
        session.scalars(
            select(Task).where(Task.user_id == user.id).order_by(Task.id.desc()).limit(3)
        ).all()
    )
    last_activity = session.scalar(
        select(func.max(Message.created_at)).where(Message.conversation_id.in_(conversation_ids))
    )
    task_previews = []
    for row in recent_tasks:
        calls = session.scalars(
            select(ToolCall)
            .where(ToolCall.task_id == row.id)
            .order_by(ToolCall.id.desc())
            .limit(2)
        ).all()
        tools = []
        for call in calls:
            try:
                payload_data = json.loads(call.result_json)
            except (TypeError, json.JSONDecodeError):
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            try:
                params_data = json.loads(call.params_json)
            except (TypeError, json.JSONDecodeError):
                params_data = {}
            if not isinstance(params_data, dict):
                params_data = {}
            tools.append(
                {
                    "name": call.tool_name,
                    "ok": call.ok,
                    "source": str(params_data.get("path") or "")[:200],
                    "result": str(payload_data.get("payload", ""))[:200],
                }
            )
        snapshot = task_snapshot(session, row.id)
        task_previews.append(
            {
                "id": row.id,
                "goal": row.goal[:200],
                "status": row.status,
                "remaining": snapshot["remaining"],
                "tools": tools,
            }
        )
    return {
        "conversations": int(
            session.scalar(
                select(func.count(Conversation.id)).where(Conversation.user_id == user.id)
            )
            or 0
        ),
        "messages": int(
            session.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id.in_(conversation_ids)
                )
            )
            or 0
        ),
        "tasks": int(
            session.scalar(select(func.count(Task.id)).where(Task.user_id == user.id)) or 0
        ),
        "tool_calls": int(
            session.scalar(
                select(func.count(ToolCall.id)).where(ToolCall.task_id.in_(task_ids))
            )
            or 0
        ),
        "summary": latest.summary if latest else None,
        "recent_messages": [
            {"role": row.role, "content": row.content[:300]} for row in recent
        ],
        "recent_tasks": task_previews,
        "last_activity": last_activity.isoformat() if last_activity else None,
        "usage": user_usage(session, user),
    }


def wipe_session(session: Session, user: User) -> dict[str, int]:
    """Xóa child-first memory có ownership, giữ User và LLM accounting."""
    conversation_ids = list(
        session.scalars(select(Conversation.id).where(Conversation.user_id == user.id)).all()
    )
    task_ids = list(session.scalars(select(Task.id).where(Task.user_id == user.id)).all())

    if task_ids:
        linked_calls = list(
            session.scalars(select(LLMCall).where(LLMCall.task_id.in_(task_ids))).all()
        )
        for call in linked_calls:
            if call.user_id is not None and call.user_id != user.id:
                raise ValueError("LLMCall có ownership mâu thuẫn; từ chối xóa")
            call.user_id = user.id
            call.task_id = None
        session.execute(delete(Step).where(Step.task_id.in_(task_ids)))
        session.execute(delete(ToolCall).where(ToolCall.task_id.in_(task_ids)))
    task_id_set = set(task_ids)
    conversation_id_set = set(conversation_ids)
    trace_filter = []
    if task_ids:
        trace_filter.append(Trace.task_id.in_(task_ids))
    if conversation_ids:
        trace_filter.append(Trace.conversation_id.in_(conversation_ids))
    if trace_filter:
        traces = list(session.scalars(select(Trace).where(or_(*trace_filter))).all())
        for trace in traces:
            if trace.task_id is None or trace.conversation_id is None:
                continue
            task_owned = trace.task_id in task_id_set
            conversation_owned = trace.conversation_id in conversation_id_set
            if task_owned != conversation_owned:
                raise ValueError("Trace có ownership mâu thuẫn; từ chối xóa")
    if trace_filter:
        session.execute(delete(Trace).where(or_(*trace_filter)))
    if task_ids:
        session.execute(delete(Task).where(Task.id.in_(task_ids)))
    if conversation_ids:
        session.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    legacy_orphans = int(
        session.scalar(select(func.count(ToolCall.id)).where(ToolCall.task_id.is_(None))) or 0
    )
    return {
        "conversations": len(conversation_ids),
        "tasks": len(task_ids),
        "legacy_unowned_tool_calls": legacy_orphans,
    }
