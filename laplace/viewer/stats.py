"""Aggregate task/tool/LLM statistics with SQL (no per-row Python loops) and CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from laplace.models import LLMCall, Step, Task, ToolCall

TERMINAL_OK = "completed"


def _task_conditions(date_from: datetime | None, date_to: datetime | None) -> list[Any]:
    conditions = []
    if date_from:
        conditions.append(Task.created_at >= date_from)
    if date_to:
        conditions.append(Task.created_at < date_to)
    return conditions


def task_stats(session: Session, *, date_from: datetime | None, date_to: datetime | None
               ) -> dict[str, Any]:
    conds = _task_conditions(date_from, date_to)
    task_ids = select(Task.id).where(*conds)
    total = session.scalar(select(func.count(Task.id)).where(*conds)) or 0
    by_status = session.execute(
        select(Task.status, func.count(Task.id)).where(*conds).group_by(Task.status)
        .order_by(Task.status)
    ).all()
    completed = dict(by_status).get(TERMINAL_OK, 0)
    by_verification = session.execute(
        select(func.coalesce(Task.verification_status, "—"), func.count(Task.id)).where(*conds)
        .group_by(Task.verification_status).order_by(Task.verification_status)
    ).all()
    step_count = session.scalar(
        select(func.count(Step.id)).where(Step.task_id.in_(task_ids))
    ) or 0
    tools = session.execute(
        select(ToolCall.tool_name, func.count(ToolCall.id),
               func.sum(case((ToolCall.ok.is_(False), 1), else_=0)),
               func.avg(ToolCall.latency_ms))
        .where(ToolCall.task_id.in_(task_ids)).group_by(ToolCall.tool_name)
        .order_by(ToolCall.tool_name)
    ).all()
    # All model spend in the date range, including plain-chat/compaction calls
    # that carry no task_id; the per-day table below is task-attributed cost.
    llm_conds = []
    if date_from:
        llm_conds.append(LLMCall.created_at >= date_from)
    if date_to:
        llm_conds.append(LLMCall.created_at < date_to)
    llm = session.execute(
        select(func.count(LLMCall.id), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
               func.coalesce(func.sum(LLMCall.completion_tokens), 0),
               func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
               func.coalesce(func.sum(LLMCall.budget_charge_usd), 0.0))
        .where(*llm_conds)
    ).one()
    day = func.date(Task.created_at)
    cost_per_task = (
        select(LLMCall.task_id, func.sum(LLMCall.cost_usd).label("cost"))
        .group_by(LLMCall.task_id).subquery()
    )
    by_day = session.execute(
        select(day, func.count(Task.id),
               func.sum(case((Task.status == TERMINAL_OK, 1), else_=0)),
               func.coalesce(func.sum(cost_per_task.c.cost), 0.0))
        .outerjoin(cost_per_task, cost_per_task.c.task_id == Task.id)
        .where(*conds).group_by(day).order_by(day)
    ).all()
    return {
        "total": total, "completed": completed,
        "completion_rate": (completed / total) if total else 0.0,
        "by_status": by_status, "by_verification": by_verification,
        "avg_steps": (step_count / total) if total else 0.0,
        "tools": [(name, int(calls), int(failed or 0), float(latency or 0))
                  for name, calls, failed, latency in tools],
        "llm": {"calls": int(llm[0]), "prompt_tokens": int(llm[1]),
                "completion_tokens": int(llm[2]), "cost_usd": float(llm[3]),
                "budget_charge_usd": float(llm[4])},
        "by_day": [(str(d), int(n), int(c or 0), float(cost or 0)) for d, n, c, cost in by_day],
    }


def stats_csv(by_day: list[tuple[str, int, int, float]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["day", "tasks", "completed", "completion_rate", "llm_cost_usd"])
    for day, tasks, completed, cost in by_day:
        writer.writerow([day, tasks, completed, f"{completed / tasks:.3f}" if tasks else "0",
                         f"{cost:.6f}"])
    return buffer.getvalue()
