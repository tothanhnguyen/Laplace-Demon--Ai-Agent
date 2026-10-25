"""Honest startup recovery: mark tasks the previous process abandoned, tell the owner.

No task is resumed or replayed. Each unfinished task becomes ``interrupted``, its
committed receipts are re-checked against the database by the verifier, and the
owner receives one five-part notice (at most once; a crash between the commit and
the send loses the notice but never the state).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from laplace import repo
from laplace.db import session_scope
from laplace.models import Conversation
from laplace.services import approvals, chat, verifier
from laplace.services.answer_format import (
    event_limitation,
    render_task_reply,
    stop_text,
    stored_task_message,
)

MAX_NOTICES = 50
GOAL_CHARS = 120


@dataclass(frozen=True, slots=True)
class InterruptedNotice:
    task_id: int
    telegram_user_id: int
    text: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    approvals: list[approvals.ApprovalRecord]
    notices: list[InterruptedNotice]
    marked: int


def _goal_hint(goal: str) -> str:
    compact = " ".join(goal.split())
    return compact if len(compact) <= GOAL_CHARS else compact[: GOAL_CHARS - 1] + "…"


def _notice_for(snapshot: dict[str, Any]) -> InterruptedNotice:
    task_id = int(snapshot["id"])
    with session_scope() as session:
        receipts = [
            verifier.Receipt(tool_name, data)
            for tool_name, data in repo.task_receipts(session, task_id)
        ]
    report = verifier.verify_task(
        task_id=task_id, user_id=int(snapshot["user_id"]), receipts=receipts
    )
    limitations = [
        event_limitation("restart_interrupted") or "",
        "Phần việc chưa có receipt ở trên đã không được thực hiện; đề xuất chưa duyệt đã vô hiệu.",
        f"Gửi lại yêu cầu nếu vẫn cần: {_goal_hint(str(snapshot['goal']))}",
    ]
    result = stop_text("restart_interrupted")
    text = render_task_reply(result=result, report=report, limitations=limitations)
    with session_scope() as session:
        step_index = repo.next_step_index(session, task_id)
        repo.append_step(
            session,
            task_id=task_id,
            step_index=step_index,
            name="restart_interrupted",
            status="interrupted",
            detail={
                "action": None,
                "observation": None,
                "task_state": {"acceptance_criteria": None, "remaining_work": None},
                "stop_reason": "restart_interrupted",
            },
        )
        repo.save_verification(
            session, task_id, status=report.status, detail_json=report.to_json()
        )
        repo.record_trace(
            session,
            task_id=task_id,
            conversation_id=snapshot.get("conversation_id"),
            step_index=step_index,
            payload={
                "event": "restart_interrupted",
                "status": "interrupted",
                "checks": len(report.checks),
                "passed": sum(1 for check in report.checks if check.ok),
            },
        )
        conversation_id = snapshot.get("conversation_id")
        conversation = (
            session.get(Conversation, conversation_id) if conversation_id is not None else None
        )
        if conversation is not None:
            repo.add_message(
                session, conversation, "assistant", stored_task_message(result, report)
            )
    return InterruptedNotice(task_id, int(snapshot["telegram_user_id"]), text)


def recover_after_restart() -> RecoveryResult:
    """Run once at startup before polling; safe to call again (second run is a no-op)."""
    with session_scope() as session:
        snapshots = repo.active_task_snapshots(session)
    interrupted = chat.interrupt_paused_turns()
    with session_scope() as session:
        marked = repo.interrupt_active_tasks(session, [item["id"] for item in snapshots])
    notices = [_notice_for(snapshot) for snapshot in snapshots[:MAX_NOTICES]]
    return RecoveryResult(interrupted, notices, marked)
