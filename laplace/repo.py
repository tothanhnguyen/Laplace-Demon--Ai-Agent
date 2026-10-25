"""Helper truy vấn/ghi DB nhận Session làm tham số đầu, không tự commit."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from laplace.logging import safe_event
from laplace.models import (
    Conversation,
    LLMCall,
    Message,
    Reminder,
    Step,
    Task,
    ToolApproval,
    ToolCall,
    Trace,
    User,
    utcnow,
)

DB_EXCERPT_CHARS = 1_200


class ReminderDeliveryInProgress(RuntimeError):
    """Raised when memory deletion races an already claimed delivery."""


class ApprovalTransitionConflict(RuntimeError):
    """The approval/task pair changed; the caller must roll back."""


def _transition_approval_task(
    session: Session, approval: ToolApproval, *, source: str, status: str
) -> None:
    changed = session.execute(
        update(Task)
        .where(
            Task.id == approval.task_id,
            Task.user_id == approval.user_id,
            Task.status == source,
        )
        .values(status=status, updated_at=utcnow())
    )
    if changed.rowcount != 1:
        raise ApprovalTransitionConflict("Approval task transition did not win.")


def create_tool_approval(
    session: Session,
    *,
    token: str,
    user_id: int,
    task_id: int,
    step_index: int,
    tool_name: str,
    risk_level: str,
    params_json: str,
    public_args_hash: str,
    resource_fingerprint: str | None,
    preview_json: str,
    created_at: int,
    expires_at: int,
    chat_id: int,
) -> ToolApproval:
    approval = ToolApproval(
        token=token,
        user_id=user_id,
        task_id=task_id,
        step_index=step_index,
        tool_name=tool_name,
        risk_level=risk_level,
        params_json=params_json,
        public_args_hash=public_args_hash,
        resource_fingerprint=resource_fingerprint,
        preview_json=preview_json,
        status="pending",
        created_at=created_at,
        expires_at=expires_at,
        chat_id=chat_id,
    )
    session.add(approval)
    session.flush()
    _transition_approval_task(
        session, approval, source="running", status="awaiting_approval"
    )
    return approval


def find_tool_approval(
    session: Session, token: str, *, telegram_user_id: int | None = None
) -> ToolApproval | None:
    query = select(ToolApproval).where(ToolApproval.token == token)
    if telegram_user_id is not None:
        query = query.join(User, ToolApproval.user_id == User.id).where(
            User.telegram_user_id == telegram_user_id
        )
    return session.scalar(query)


def bind_approval_preview(
    session: Session,
    approval_id: int,
    *,
    chat_id: int,
    message_id: int,
    now_utc: int,
) -> bool:
    if message_id <= 0:
        return False
    task_awaiting = select(Task.id).where(
        Task.id == ToolApproval.task_id,
        Task.user_id == ToolApproval.user_id,
        Task.status == "awaiting_approval",
    ).exists()
    private_owner = select(User.id).where(
        User.id == ToolApproval.user_id, User.telegram_user_id == chat_id
    ).exists()
    changed = session.execute(
        update(ToolApproval)
        .where(
            ToolApproval.id == approval_id,
            ToolApproval.status == "pending",
            ToolApproval.chat_id == chat_id,
            ToolApproval.preview_message_id.is_(None),
            ToolApproval.expires_at > now_utc,
            task_awaiting,
            private_owner,
        )
        .values(preview_message_id=message_id)
    )
    return changed.rowcount == 1


def claim_tool_approval(
    session: Session,
    token: str,
    *,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
    now_utc: int,
) -> ToolApproval | None:
    if chat_id != telegram_user_id or message_id <= 0:
        return None
    approval = session.scalar(
        select(ToolApproval)
        .join(User, User.id == ToolApproval.user_id)
        .join(Task, Task.id == ToolApproval.task_id)
        .where(
            ToolApproval.token == token,
            ToolApproval.status == "pending",
            ToolApproval.chat_id == chat_id,
            ToolApproval.preview_message_id == message_id,
            ToolApproval.expires_at > now_utc,
            User.telegram_user_id == telegram_user_id,
            Task.user_id == ToolApproval.user_id,
            Task.status == "awaiting_approval",
        )
    )
    if approval is None:
        return None
    changed = session.execute(
        update(ToolApproval)
        .where(
            ToolApproval.id == approval.id,
            ToolApproval.token == token,
            ToolApproval.user_id == approval.user_id,
            ToolApproval.task_id == approval.task_id,
            ToolApproval.user_id.in_(
                select(User.id).where(User.telegram_user_id == telegram_user_id)
            ),
            ToolApproval.status == "pending",
            ToolApproval.chat_id == chat_id,
            ToolApproval.preview_message_id == message_id,
            ToolApproval.expires_at > now_utc,
        )
        .values(status="approved", decided_at=now_utc)
    )
    if changed.rowcount != 1:
        return None
    _transition_approval_task(session, approval, source="awaiting_approval", status="running")
    session.refresh(approval)
    return approval


def resolve_tool_approval(
    session: Session,
    token: str,
    *,
    status: str,
    now_utc: int,
    telegram_user_id: int | None = None,
    task_status: str | None = None,
    allowed_statuses: tuple[str, ...] = ("pending",),
) -> ToolApproval | None:
    terminal_tasks = {
        "rejected": "rejected",
        "expired": "expired",
        "cancelled": "cancelled",
        "interrupted": "interrupted",
        "invalidated": "failed",
        "failed": "failed",
    }
    if status not in terminal_tasks or not set(allowed_statuses) <= {"pending", "approved"}:
        raise ValueError("Unsupported approval transition.")
    approval = find_tool_approval(session, token, telegram_user_id=telegram_user_id)
    if approval is None or approval.status not in allowed_statuses:
        return None
    source = approval.status
    conditions = [
        ToolApproval.id == approval.id,
        ToolApproval.token == token,
        ToolApproval.status == source,
    ]
    if telegram_user_id is not None:
        conditions.append(
            ToolApproval.user_id.in_(
                select(User.id).where(User.telegram_user_id == telegram_user_id)
            )
        )
    if status == "expired":
        conditions.append(ToolApproval.expires_at <= now_utc)
    changed = session.execute(
        update(ToolApproval)
        .where(*conditions)
        .values(status=status, decided_at=now_utc)
    )
    if changed.rowcount != 1:
        return None
    _transition_approval_task(
        session,
        approval,
        source="awaiting_approval" if source == "pending" else "running",
        status=task_status or terminal_tasks[status],
    )
    session.refresh(approval)
    return approval


def expired_tool_approvals(
    session: Session, now_utc: int, limit: int = 25
) -> list[ToolApproval]:
    if limit <= 0:
        return []
    return list(
        session.scalars(
            select(ToolApproval)
            .where(ToolApproval.status == "pending", ToolApproval.expires_at <= now_utc)
            .order_by(ToolApproval.expires_at, ToolApproval.id)
            .limit(min(limit, 25))
        ).all()
    )


def interrupt_tool_approvals(session: Session, now_utc: int) -> list[ToolApproval]:
    active = list(
        session.scalars(
            select(ToolApproval)
            .where(ToolApproval.status.in_(("pending", "approved")))
            .order_by(ToolApproval.id)
        ).all()
    )
    affected = []
    for approval in active:
        resolved = resolve_tool_approval(
            session,
            approval.token,
            status="interrupted",
            now_utc=now_utc,
            allowed_statuses=("pending", "approved"),
        )
        if resolved is not None:
            affected.append(resolved)
    executed = list(
        session.scalars(
            select(ToolApproval)
            .join(Task, Task.id == ToolApproval.task_id)
            .where(
                ToolApproval.status == "executed",
                Task.user_id == ToolApproval.user_id,
                Task.status.in_(("pending", "running", "awaiting_approval")),
            )
            .order_by(ToolApproval.id)
        ).all()
    )
    for approval in executed:
        changed = session.execute(
            update(Task)
            .where(
                Task.id == approval.task_id,
                Task.user_id == approval.user_id,
                Task.status.in_(("pending", "running", "awaiting_approval")),
            )
            .values(status="interrupted", updated_at=utcnow())
        )
        if changed.rowcount == 1:
            affected.append(approval)
    return affected


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


def recent_messages(session: Session, conversation: Conversation, limit: int = 10) -> list[Message]:
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
        "verification_status": task.verification_status,
        "decision_index": steps[-1].step_index if steps else 0,
        "tool_count": tool_count,
        "tools": [
            {"name": name, "count": int(count), "failed": int(failed or 0)}
            for name, count, failed in tool_rows
        ],
        "criteria": criteria,
        "remaining": remaining,
    }


ACTIVE_TASK_STATUSES = ("pending", "running", "awaiting_approval")


def active_task_snapshots(session: Session, *, limit: int = 1_000) -> list[dict[str, Any]]:
    """Tasks a crashed process left unfinished, oldest first; read before interrupting."""
    rows = session.execute(
        select(Task.id, Task.user_id, User.telegram_user_id, Task.conversation_id, Task.goal,
               Task.status)
        .join(User, User.id == Task.user_id)
        .where(Task.status.in_(ACTIVE_TASK_STATUSES))
        .order_by(Task.id)
        .limit(limit)
    ).all()
    return [
        {
            "id": task_id, "user_id": user_id, "telegram_user_id": telegram_user_id,
            "conversation_id": conversation_id, "goal": goal, "status": status,
        }
        for task_id, user_id, telegram_user_id, conversation_id, goal, status in rows
    ]


def interrupt_active_tasks(session: Session, task_ids: Sequence[int]) -> int:
    """CAS every listed task that is still active to ``interrupted``; returns the count."""
    if not task_ids:
        return 0
    changed = session.execute(
        update(Task)
        .where(Task.id.in_(list(task_ids)), Task.status.in_(ACTIVE_TASK_STATUSES))
        .values(status="interrupted", updated_at=utcnow())
    )
    return int(changed.rowcount or 0)


def task_receipts(session: Session, task_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Committed (executed) approval receipts of a task as ``(tool_name, data)`` pairs."""
    rows = session.scalars(
        select(ToolApproval)
        .where(ToolApproval.task_id == task_id, ToolApproval.status == "executed")
        .order_by(ToolApproval.id)
    ).all()
    receipts: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        try:
            envelope = json.loads(row.result_json or "")
        except (TypeError, json.JSONDecodeError):
            continue
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if isinstance(data, dict):
            receipts.append((row.tool_name, data))
    return receipts


def next_step_index(session: Session, task_id: int) -> int:
    current = session.scalar(select(func.max(Step.step_index)).where(Step.task_id == task_id))
    return 0 if current is None else int(current) + 1


def task_tool_results(session: Session, task_id: int) -> list[dict[str, Any]]:
    """Parsed canonical tool envelopes of one task, oldest first, for verification."""
    rows = session.scalars(
        select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.id)
    ).all()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            envelope = json.loads(row.result_json)
        except (TypeError, json.JSONDecodeError):
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        sources = envelope.get("sources")
        results.append(
            {
                "tool_call_id": row.id,
                "tool_name": row.tool_name,
                "ok": bool(row.ok),
                "error_code": envelope.get("error_code"),
                "sources": [str(item) for item in sources] if isinstance(sources, list) else [],
                "data": envelope.get("data"),
            }
        )
    return results


def save_verification(
    session: Session, task_id: int, *, status: str, detail_json: str
) -> None:
    """Persist the evidence verdict on its task; the task status axis is untouched."""
    session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(verification_status=status, verification_json=detail_json)
    )


def tool_rows_for_request_ids(
    session: Session, request_message_ids: list[int]
) -> list[dict[str, Any]]:
    """Read bounded canonical tool projections without loading full payloads."""

    if not request_message_ids:
        return []
    valid_json = func.json_valid(ToolCall.result_json) == 1
    sources_json = case(
        (valid_json, func.coalesce(func.json_extract(ToolCall.result_json, "$.sources"), "[]")),
        else_="[]",
    )
    data_json = case(
        (
            valid_json,
            func.coalesce(
                func.json_extract(ToolCall.result_json, "$.data"),
                func.json_extract(ToolCall.result_json, "$.payload"),
                "",
            ),
        ),
        else_="",
    )
    rows = session.execute(
        select(
            ToolCall.id,
            Task.request_message_id,
            ToolCall.tool_name,
            ToolCall.ok,
            sources_json,
            func.length(data_json),
            func.substr(data_json, 1, DB_EXCERPT_CHARS),
            func.substr(data_json, -DB_EXCERPT_CHARS),
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
        raw_sources,
        data_chars,
        data_prefix,
        data_suffix,
    ) in rows:
        try:
            decoded_sources = json.loads(raw_sources or "[]")
        except (TypeError, json.JSONDecodeError):
            decoded_sources = []
        sources = (
            [str(source) for source in decoded_sources if str(source).strip()]
            if isinstance(decoded_sources, list)
            else []
        )
        total_chars = int(data_chars or 0)
        result.append(
            {
                "id": call_id,
                "request_message_id": request_id,
                "tool_name": tool_name,
                "ok": ok,
                "sources": sources,
                "payload": _bounded_db_excerpt(
                    data_prefix,
                    data_suffix,
                    total_chars,
                ),
            }
        )
    return result


_ACCOUNTING_DETAIL_KEYS = {
    "requested_model",
    "returned_model",
    "estimator_version",
    "pricing_version",
    "input_price_per_million",
    "output_price_per_million",
    "outcome",
    "usage_known",
}
_ATTEMPT_OUTCOMES = {
    "reserved",
    "success",
    "rate_limit",
    "format_rejected",
    "timeout",
    "error",
    "interrupted",
}


def _validated_accounting_detail(detail: str | None) -> str | None:
    """Keep accounting metadata separate from prompts, responses, and raw errors."""
    if detail is None:
        return None
    try:
        values = json.loads(detail)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Accounting detail must be a JSON object.") from exc
    if not isinstance(values, dict) or not values.keys() <= _ACCOUNTING_DETAIL_KEYS:
        raise ValueError("Unsupported accounting detail fields.")
    for key, value in values.items():
        if key == "usage_known":
            if not isinstance(value, bool):
                raise ValueError("Accounting usage_known must be boolean.")
        elif key == "returned_model" and value is None:
            continue
        elif not isinstance(value, str):
            raise ValueError("Accounting metadata must use string representations.")
        elif key == "outcome" and value not in _ATTEMPT_OUTCOMES:
            raise ValueError("Unsupported accounting attempt outcome.")
        elif key in {"input_price_per_million", "output_price_per_million"}:
            try:
                rate = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("Accounting prices must be finite and nonnegative.") from exc
            if not rate.is_finite() or rate < 0:
                raise ValueError("Accounting prices must be finite and nonnegative.")
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def record_llm_call(
    session: Session,
    *,
    task_id: int | None = None,
    user_id: int | None = None,
    request_message_id: int | None = None,
    call_group_id: str | None = None,
    attempt_index: int | None = None,
    accounting_status: str | None = None,
    budget_charge_usd: float | None = None,
    accounting_detail: str | None = None,
    purpose: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    latency_ms: int,
) -> LLMCall:
    """Ghi usage LLM; user_id trực tiếp bảo toàn accounting khi task bị xóa."""
    if accounting_status not in {None, "reserved", "settled", "unknown"}:
        raise ValueError("Unsupported attempt accounting status.")
    accounting_detail = _validated_accounting_detail(accounting_detail)
    call = LLMCall(
        task_id=task_id,
        user_id=user_id,
        request_message_id=request_message_id,
        call_group_id=call_group_id,
        attempt_index=attempt_index,
        accounting_status=accounting_status,
        budget_charge_usd=budget_charge_usd,
        accounting_detail=accounting_detail,
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


def finalize_llm_attempt(
    session: Session,
    attempt_id: int,
    *,
    accounting_status: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    budget_charge_usd: float,
    latency_ms: int,
    accounting_detail: str,
) -> bool:
    """Finalize a reservation once; transaction ownership remains with the caller."""
    if accounting_status not in {"settled", "unknown"}:
        raise ValueError("Unsupported attempt accounting transition.")
    settled = accounting_status == "settled"
    accounting_detail = _validated_accounting_detail(accounting_detail)
    result = session.execute(
        update(LLMCall)
        .where(LLMCall.id == attempt_id, LLMCall.accounting_status == "reserved")
        .values(
            accounting_status=accounting_status,
            model=model,
            prompt_tokens=prompt_tokens if settled else 0,
            completion_tokens=completion_tokens if settled else 0,
            cost_usd=cost_usd if settled else 0.0,
            budget_charge_usd=budget_charge_usd if settled else LLMCall.budget_charge_usd,
            latency_ms=latency_ms,
            accounting_detail=accounting_detail,
        )
    )
    return result.rowcount == 1


def attach_request_llm_calls(
    session: Session, *, user_id: int, request_message_id: int, task_id: int
) -> int:
    """Attribute pre-task calls to the same owner and exact request, never latest."""
    task_matches = select(Task.id).where(
        Task.id == task_id,
        Task.user_id == user_id,
        Task.request_message_id == request_message_id,
    ).exists()
    request_matches = (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.id == request_message_id, Conversation.user_id == user_id)
        .exists()
    )
    result = session.execute(
        update(LLMCall)
        .where(
            LLMCall.user_id == user_id,
            LLMCall.request_message_id == request_message_id,
            LLMCall.task_id.is_(None),
            task_matches,
            request_matches,
        )
        .values(task_id=task_id)
    )
    return int(result.rowcount or 0)


def interrupt_reserved_llm_calls(session: Session) -> int:
    """Mark crash-interrupted attempts unknown without refunding or replaying."""
    rows = session.execute(
        select(LLMCall.id, LLMCall.accounting_detail).where(
            LLMCall.accounting_status == "reserved"
        )
    ).all()
    changed = 0
    for attempt_id, raw_detail in rows:
        try:
            detail = json.loads(raw_detail) if raw_detail is not None else {}
            if not isinstance(detail, dict):
                detail = {}
            detail = {
                key: value for key, value in detail.items() if key in _ACCOUNTING_DETAIL_KEYS
            }
            _validated_accounting_detail(json.dumps(detail))
        except (TypeError, ValueError):
            detail = {}
        detail.update(outcome="interrupted", usage_known=False)
        result = session.execute(
            update(LLMCall)
            .where(LLMCall.id == attempt_id, LLMCall.accounting_status == "reserved")
            .values(
                accounting_status="unknown",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                accounting_detail=_validated_accounting_detail(json.dumps(detail)),
            )
        )
        changed += int(result.rowcount or 0)
    return changed


def record_tool_call(
    session: Session,
    *,
    task_id: int | None = None,
    approval_id: int | None = None,
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
        approval_id=approval_id,
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
    """Persist only allowlisted audit metadata; canonical evidence stays private."""
    trace = Trace(
        task_id=task_id,
        conversation_id=conversation_id,
        step_index=step_index,
        payload_json=json.dumps(
            safe_event({**payload, "task_id": task_id, "step_index": step_index}),
            ensure_ascii=False,
        ),
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


def _llm_usage(session: Session, *conditions: Any) -> dict[str, Any]:
    """Project logical calls, known usage, and conservative attempt charges."""
    known = or_(
        LLMCall.accounting_status.is_(None), LLMCall.accounting_status == "settled"
    )
    calls, physical, prompt, completion, cost, charge, unknown = session.execute(
        select(
            func.coalesce(
                func.sum(case((LLMCall.call_group_id.is_(None), 1), else_=0)), 0
            )
            + func.count(func.distinct(LLMCall.call_group_id)),
            func.count(LLMCall.id),
            func.coalesce(func.sum(case((known, LLMCall.prompt_tokens), else_=0)), 0),
            func.coalesce(func.sum(case((known, LLMCall.completion_tokens), else_=0)), 0),
            func.coalesce(func.sum(case((known, LLMCall.cost_usd), else_=0.0)), 0.0),
            func.coalesce(
                func.sum(
                    case(
                        (LLMCall.accounting_status.is_(None), LLMCall.cost_usd),
                        else_=func.coalesce(LLMCall.budget_charge_usd, 0.0),
                    )
                ),
                0.0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (LLMCall.accounting_status.in_(("reserved", "unknown")), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(*conditions)
    ).one()
    known_cost = round(float(cost), 9)
    return {
        "llm_calls": int(calls),
        "physical_attempts": int(physical),
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
        "cost_usd": round(float(cost), 6),
        "known_cost_usd": known_cost,
        "budget_charge_usd": round(float(charge), 9),
        "unknown_attempts": int(unknown),
    }


def user_usage(session: Session, user: User) -> dict[str, Any]:
    """Aggregate direct user or legacy task ownership without counting twice."""
    task_ids = select(Task.id).where(Task.user_id == user.id)
    return _llm_usage(
        session, (LLMCall.user_id == user.id) | (LLMCall.task_id.in_(task_ids))
    )


def total_usage(session: Session) -> dict[str, Any]:
    """Return the same accounting projection across all owners and legacy rows."""
    return _llm_usage(session)


def create_reminder(
    session: Session,
    *,
    user_id: int,
    message: str,
    timezone: str,
    due_at_utc: int,
) -> Reminder:
    """Create one owner-linked pending reminder."""
    reminder = Reminder(
        user_id=user_id,
        message=message,
        timezone=timezone,
        due_at_utc=due_at_utc,
        next_attempt_at_utc=due_at_utc,
        status="pending",
    )
    session.add(reminder)
    session.flush()
    return reminder


def reminder_for_owner(session: Session, user_id: int, reminder_id: int) -> Reminder | None:
    """Return a reminder only when it belongs to the trusted owner."""
    return session.scalar(
        select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
    )


def _reminder_snapshot_conditions(expected: Reminder | None) -> tuple:
    if expected is None:
        return ()
    return (
        Reminder.id == expected.id,
        Reminder.user_id == expected.user_id,
        Reminder.status == expected.status,
        Reminder.message == expected.message,
        Reminder.due_at_utc == expected.due_at_utc,
        Reminder.timezone == expected.timezone,
    )


def update_pending_reminder(
    session: Session,
    *,
    user_id: int,
    reminder_id: int,
    message: str | None,
    due_at_utc: int | None,
    timezone: str,
    expected: Reminder | None = None,
) -> Reminder | None:
    values: dict[str, Any] = {"updated_at": utcnow()}
    if message is not None:
        values["message"] = message
    if due_at_utc is None:
        allowed_statuses = ("pending",)
    else:
        allowed_statuses = ("pending", "failed", "delivery_unknown")
        values.update(
            due_at_utc=due_at_utc,
            next_attempt_at_utc=due_at_utc,
            timezone=timezone,
            status="pending",
            attempt_count=0,
            first_attempt_at_utc=None,
            last_attempt_at_utc=None,
            sent_at_utc=None,
            telegram_message_id=None,
            last_error=None,
        )
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
            Reminder.status.in_(allowed_statuses),
            *_reminder_snapshot_conditions(expected),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        return None
    return reminder_for_owner(session, user_id, reminder_id)


def cancel_pending_reminder(
    session: Session,
    *,
    user_id: int,
    reminder_id: int,
    expected: Reminder | None = None,
) -> Reminder | None:
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
            Reminder.status == "pending",
            *_reminder_snapshot_conditions(expected),
        )
        .values(status="cancelled", updated_at=utcnow())
    )
    if result.rowcount != 1:
        return None
    return reminder_for_owner(session, user_id, reminder_id)


def due_reminder_candidates(
    session: Session,
    now_utc: int,
    limit: int,
    *,
    after: tuple[int, int] | None = None,
) -> list[tuple[int, int, int]]:
    query = (
        select(Reminder.id, Reminder.next_attempt_at_utc, User.telegram_user_id)
        .join(User, User.id == Reminder.user_id)
        .where(
            Reminder.status == "pending",
            Reminder.next_attempt_at_utc <= now_utc,
        )
    )
    if after is not None:
        after_attempt, after_id = after
        query = query.where(
            or_(
                Reminder.next_attempt_at_utc > after_attempt,
                (Reminder.next_attempt_at_utc == after_attempt) & (Reminder.id > after_id),
            )
        )
    rows = session.execute(
        query.order_by(Reminder.next_attempt_at_utc, Reminder.id).limit(limit)
    ).all()
    return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]


def defer_pending_reminder(
    session: Session,
    reminder_id: int,
    *,
    eligible_at_utc: int,
    next_attempt_at_utc: int,
) -> bool:
    """Move a lease-blocked row behind the current due queue without claiming it."""
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.status == "pending",
            Reminder.next_attempt_at_utc <= eligible_at_utc,
        )
        .values(next_attempt_at_utc=next_attempt_at_utc, updated_at=utcnow())
    )
    return result.rowcount == 1


def claim_reminder(session: Session, reminder_id: int, now_utc: int) -> bool:
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.status == "pending",
            Reminder.next_attempt_at_utc <= now_utc,
        )
        .values(
            status="delivering",
            attempt_count=Reminder.attempt_count + 1,
            first_attempt_at_utc=case(
                (Reminder.first_attempt_at_utc.is_(None), now_utc),
                else_=Reminder.first_attempt_at_utc,
            ),
            last_attempt_at_utc=now_utc,
            updated_at=utcnow(),
        )
    )
    return result.rowcount == 1


def mark_reminder_sent(
    session: Session,
    reminder_id: int,
    *,
    now_utc: int,
    telegram_message_id: int,
) -> bool:
    result = session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.status == "delivering")
        .values(
            status="sent",
            sent_at_utc=now_utc,
            telegram_message_id=telegram_message_id,
            last_error=None,
            updated_at=utcnow(),
        )
    )
    return result.rowcount == 1


def transition_reminder_failure(
    session: Session,
    reminder_id: int,
    *,
    now_utc: int,
    error: str,
    retry_at_utc: int,
    max_attempts: int,
    grace_seconds: int,
    retryable: bool,
) -> str | None:
    reminder = session.scalar(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.status == "delivering",
        )
    )
    if reminder is None:
        return None
    within_grace = now_utc <= reminder.due_at_utc + grace_seconds
    retry = retryable and reminder.attempt_count < max_attempts and within_grace
    if retry:
        reminder.status = "pending"
        reminder.next_attempt_at_utc = min(
            retry_at_utc,
            reminder.due_at_utc + grace_seconds,
        )
    else:
        reminder.status = "failed" if not retryable else "delivery_unknown"
    reminder.last_error = error[:500]
    reminder.updated_at = utcnow()
    return reminder.status


def recover_stale_reminders(
    session: Session,
    now_utc: int,
    max_attempts: int,
    grace_seconds: int,
) -> int:
    reminders = list(session.scalars(select(Reminder).where(Reminder.status == "delivering")).all())
    for reminder in reminders:
        retry = (
            reminder.attempt_count < max_attempts and now_utc <= reminder.due_at_utc + grace_seconds
        )
        reminder.status = "pending" if retry else "delivery_unknown"
        if retry:
            reminder.next_attempt_at_utc = now_utc
        reminder.last_error = "Recovered an interrupted delivery attempt."
        reminder.updated_at = utcnow()
    return len(reminders)


def mark_missed_reminders(session: Session, cutoff_utc: int) -> int:
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.status == "pending",
            Reminder.due_at_utc < cutoff_utc,
        )
        .values(
            status="missed",
            updated_at=utcnow(),
            last_error="Reminder expired during downtime.",
        )
    )
    return int(result.rowcount or 0)


def session_stats(session: Session, user: User) -> dict[str, Any]:
    """Read bounded owner-scoped memory and reminder previews."""
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
    recent_reminders = list(
        session.scalars(
            select(Reminder)
            .where(
                Reminder.user_id == user.id,
                Reminder.status.in_(("pending", "delivering")),
            )
            .order_by(Reminder.due_at_utc, Reminder.id)
            .limit(3)
        ).all()
    )
    reminder_total = int(
        session.scalar(select(func.count(Reminder.id)).where(Reminder.user_id == user.id)) or 0
    )
    reminder_active = int(
        session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.user_id == user.id,
                Reminder.status.in_(("pending", "delivering")),
            )
        )
        or 0
    )
    task_previews = []
    for row in recent_tasks:
        calls = session.scalars(
            select(ToolCall).where(ToolCall.task_id == row.id).order_by(ToolCall.id.desc()).limit(2)
        ).all()
        tools = []
        for call in calls:
            try:
                envelope = json.loads(call.result_json)
            except (TypeError, json.JSONDecodeError):
                envelope = {}
            if not isinstance(envelope, dict):
                envelope = {}
            raw_sources = envelope.get("sources")
            sources = (
                [str(source)[:200] for source in raw_sources if str(source).strip()]
                if isinstance(raw_sources, list)
                else []
            )
            data = envelope.get("data", envelope.get("payload", ""))
            result_preview = (
                data
                if isinstance(data, str)
                else json.dumps(data, ensure_ascii=False, sort_keys=True)
            )
            tools.append(
                {
                    "name": call.tool_name,
                    "ok": call.ok,
                    "sources": sources,
                    "result": result_preview[:200],
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
                select(func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids))
            )
            or 0
        ),
        "tasks": int(
            session.scalar(select(func.count(Task.id)).where(Task.user_id == user.id)) or 0
        ),
        "tool_calls": int(
            session.scalar(select(func.count(ToolCall.id)).where(ToolCall.task_id.in_(task_ids)))
            or 0
        ),
        "summary": latest.summary if latest else None,
        "recent_messages": [{"role": row.role, "content": row.content[:300]} for row in recent],
        "recent_tasks": task_previews,
        "reminders": {
            "total": reminder_total,
            "active": reminder_active,
        },
        "recent_reminders": [
            {
                "id": row.id,
                "message": row.message[:200],
                "run_at": datetime.fromtimestamp(
                    row.due_at_utc,
                    ZoneInfo(row.timezone),
                ).isoformat(timespec="seconds"),
                "timezone": row.timezone,
                "status": row.status,
            }
            for row in recent_reminders
        ],
        "last_activity": last_activity.isoformat() if last_activity else None,
        "usage": user_usage(session, user),
    }


def wipe_session(session: Session, user: User) -> dict[str, int]:
    """Xóa child-first memory có ownership, giữ User và LLM accounting."""
    conversation_ids = list(
        session.scalars(select(Conversation.id).where(Conversation.user_id == user.id)).all()
    )
    task_ids = list(session.scalars(select(Task.id).where(Task.user_id == user.id)).all())
    request_ids = list(
        session.scalars(
            select(Message.id).where(Message.conversation_id.in_(conversation_ids))
        ).all()
    )
    request_id_set = set(request_ids)
    reminder_count = int(
        session.scalar(select(func.count(Reminder.id)).where(Reminder.user_id == user.id)) or 0
    )
    session.execute(
        delete(Reminder).where(
            Reminder.user_id == user.id,
            Reminder.status != "delivering",
        )
    )
    delivering = int(
        session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.user_id == user.id,
                Reminder.status == "delivering",
            )
        )
        or 0
    )
    if delivering:
        raise ReminderDeliveryInProgress

    task_id_set = set(task_ids)
    approval_rows = list(
        session.scalars(
            select(ToolApproval).where(
                or_(ToolApproval.user_id == user.id, ToolApproval.task_id.in_(task_ids))
            )
        ).all()
    )
    for approval in approval_rows:
        if approval.user_id != user.id or approval.task_id not in task_id_set:
            raise ValueError("ToolApproval có ownership mâu thuẫn; từ chối xóa")
    approval_ids = [approval.id for approval in approval_rows]
    tool_filter = or_(
        ToolCall.task_id.in_(task_ids), ToolCall.approval_id.in_(approval_ids)
    )
    for call in session.scalars(select(ToolCall).where(tool_filter)):
        if (
            call.task_id is not None and call.task_id not in task_id_set
            or call.approval_id is not None and call.approval_id not in approval_ids
        ):
            raise ValueError("ToolCall có ownership mâu thuẫn; từ chối xóa")
    linked_calls = list(
        session.scalars(
            select(LLMCall).where(
                or_(
                    LLMCall.user_id == user.id,
                    LLMCall.task_id.in_(task_ids),
                    LLMCall.request_message_id.in_(request_ids),
                )
            )
        ).all()
    )
    for call in linked_calls:
        if (
            call.user_id is not None and call.user_id != user.id
            or call.task_id is not None and call.task_id not in task_id_set
            or call.request_message_id is not None
            and call.request_message_id not in request_id_set
        ):
            raise ValueError("LLMCall có ownership mâu thuẫn; từ chối xóa")
        call.user_id = user.id
        call.task_id = None
        call.request_message_id = None
    if task_ids:
        session.execute(delete(Step).where(Step.task_id.in_(task_ids)))
        session.execute(delete(ToolCall).where(tool_filter))
        session.execute(delete(ToolApproval).where(ToolApproval.id.in_(approval_ids)))
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
        "reminders": reminder_count,
    }
