"""Read-only task queries for the observation pages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from laplace.models import LLMCall, Message, Step, Task, ToolCall, Trace, User
from laplace.viewer.common import PAGE_SIZE, parse_json


def list_tasks(
    session: Session, *, status: str | None, date_from: datetime | None,
    date_to: datetime | None, page: int,
) -> dict[str, Any]:
    conditions = []
    if status:
        conditions.append(Task.status == status)
    if date_from:
        conditions.append(Task.created_at >= date_from)
    if date_to:
        conditions.append(Task.created_at < date_to)
    total = session.scalar(select(func.count(Task.id)).where(*conditions)) or 0
    rows = session.execute(
        select(Task, User.username, User.telegram_user_id)
        .join(User, User.id == Task.user_id).where(*conditions)
        .order_by(Task.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    tasks = [{"task": task, "username": username, "telegram_user_id": telegram_id}
             for task, username, telegram_id in rows]
    statuses = [s for (s,) in session.execute(select(Task.status).distinct().order_by(Task.status))]
    return {"tasks": tasks, "total": total, "page": page,
            "pages": max(1, -(-total // PAGE_SIZE)), "statuses": statuses}


def _step_view(step: Step) -> dict[str, Any]:
    detail = parse_json(step.detail) or {}
    action = detail.get("action") if isinstance(detail, dict) else None
    return {
        "row": step,
        "action": action if isinstance(action, dict) else None,
        "observation": detail.get("observation") if isinstance(detail, dict) else None,
        "stop_reason": detail.get("stop_reason") if isinstance(detail, dict) else None,
        "task_state": detail.get("task_state") if isinstance(detail, dict) else None,
        "event": detail.get("event") if isinstance(detail, dict) else None,
    }


def task_detail(session: Session, task_id: int) -> dict[str, Any] | None:
    task = session.get(Task, task_id)
    if task is None:
        return None
    user = session.get(User, task.user_id)
    steps = session.scalars(select(Step).where(Step.task_id == task_id).order_by(Step.id)).all()
    tool_calls = session.scalars(
        select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.id)
    ).all()
    llm_calls = session.scalars(
        select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.id)
    ).all()
    traces = session.scalars(
        select(Trace).where(Trace.task_id == task_id).order_by(Trace.id)
    ).all()
    request = session.get(Message, task.request_message_id) if task.request_message_id else None
    reply = None
    if request is not None:
        reply = session.scalars(
            select(Message).where(
                Message.conversation_id == request.conversation_id, Message.id > request.id,
                Message.role == "assistant",
            ).order_by(Message.id)
        ).first()
    verification = parse_json(task.verification_json) or {}
    return {
        "task": task, "user": user, "request_message": request, "reply_message": reply,
        "steps": [_step_view(step) for step in steps],
        "tool_calls": tool_calls, "llm_calls": llm_calls,
        "traces": [{"row": trace, "payload": parse_json(trace.payload_json)} for trace in traces],
        "verification_checks": verification.get("checks", []) if isinstance(verification, dict)
        else [],
        "llm_cost": sum(call.cost_usd for call in llm_calls),
        "llm_tokens": sum(call.prompt_tokens + call.completion_tokens for call in llm_calls),
    }
