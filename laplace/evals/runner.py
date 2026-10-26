"""Run evaluation cases through the shared chat runtime on an isolated SQLite file.

One run owns one database; each case owns one synthetic Telegram user, so the
oracle queries are scoped by user and never leak between cases. Nothing in the
runtime is mocked except the LLM provider (mock mode) and injected tool faults.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from laplace import repo
from laplace.config import Settings, configured_secrets
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.evals.cases import EvalCase
from laplace.evals.checks import CheckResult, evaluate, failure_group
from laplace.evals.faults import install_faults
from laplace.llm.base import get_provider, resolve_provider_config
from laplace.llm.mock import MockLLM
from laplace.logging import redact_text
from laplace.models import Reminder, Step, Task, ToolApproval, ToolCall
from laplace.services import approvals, chat, memory

BASE_USER_ID = 900_000
PREVIEW_MESSAGE_ID = 1


@dataclass
class CaseResult:
    case: EvalCase
    user_id: int
    reply: str = ""
    task_id: int | None = None
    stop_reason: str | None = None
    duration_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    harness_error: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    failure_group: str | None = None

    @property
    def passed(self) -> bool:
        return self.harness_error is None and self.failure_group is None

    @property
    def cost_usd(self) -> float:
        return float(self.usage.get("known_cost_usd", 0.0) or 0.0)


@dataclass
class SuiteResult:
    run_id: str
    provider_mode: str
    model: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)


SEEDED_REMINDER = "$seeded_reminder_id"


def _substitute(value: Any, reminder_id: int | None) -> Any:
    """Replace the seeded-reminder placeholder anywhere in text or script."""
    if isinstance(value, str):
        if SEEDED_REMINDER in value and reminder_id is None:
            raise ValueError("case uses $seeded_reminder_id but seeds no reminder")
        if value == SEEDED_REMINDER:
            return reminder_id  # tool params need the integer id
        return value.replace(SEEDED_REMINDER, str(reminder_id))
    if isinstance(value, dict):
        return {k: _substitute(v, reminder_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, reminder_id) for v in value]
    return value


def _seed(user_id: int, username: str, case: EvalCase) -> int | None:
    """Seed conversation and reminders; returns the first seeded reminder id."""
    settings = chat.Settings()  # same hook the runtime reads, so test patches compose
    first_reminder: int | None = None
    with session_scope() as session:
        user = repo.get_or_create_user(session, user_id, username)
        conversation = repo.get_or_create_conversation(session, user)
        for message in case.initial_state.messages:
            repo.add_message(session, conversation, message.role, message.content)
        now = int(time.time())
        for reminder in case.initial_state.reminders:
            row = repo.create_reminder(
                session, user_id=user.id, message=reminder.message,
                timezone=settings.timezone, due_at_utc=now + reminder.delay_seconds,
            )
            session.flush()
            first_reminder = first_reminder or row.id
    return first_reminder


def _facts(user_id: int, reply: str) -> dict[str, Any]:
    """Read everything the checks need in one short transaction."""
    with session_scope() as session:
        user = repo.find_user(session, user_id)
        facts: dict[str, Any] = {
            "reply": reply, "task_id": None, "task_status": None, "stop_reason": None,
            "verification_status": None, "tool_calls": [], "active_reminders": 0,
            "approval_status": None,
        }
        if user is None:
            return facts
        task = session.scalars(
            select(Task).where(Task.user_id == user.id).order_by(Task.id.desc())
        ).first()
        if task is not None:
            facts.update(task_id=task.id, task_status=task.status,
                         verification_status=task.verification_status)
            last = session.scalars(
                select(Step).where(Step.task_id == task.id).order_by(Step.id.desc())
            ).first()
            if last is not None and last.detail:
                try:
                    facts["stop_reason"] = json.loads(last.detail).get("stop_reason")
                except (ValueError, AttributeError):
                    facts["stop_reason"] = None
            facts["tool_calls"] = [
                {"tool_name": c.tool_name, "ok": bool(c.ok), "approval_id": c.approval_id}
                for c in session.scalars(
                    select(ToolCall).where(ToolCall.task_id == task.id).order_by(ToolCall.id)
                )
            ]
        facts["active_reminders"] = len([
            r for r in session.scalars(select(Reminder).where(Reminder.user_id == user.id))
            if r.status != "cancelled"
        ])
        approval = session.scalars(
            select(ToolApproval).where(ToolApproval.user_id == user.id)
            .order_by(ToolApproval.id.desc())
        ).first()
        facts["approval_status"] = approval.status if approval is not None else None
        return facts


def _decide_approval(
    case: EvalCase, user_id: int, reply: chat.ChatReply, provider
) -> chat.ChatReply:
    """Simulate the Telegram button press the way bot handlers do."""
    pause = reply.pause
    if pause is None or case.approval == "none":
        return reply
    if not approvals.bind_preview(
        pause.token, telegram_user_id=user_id, chat_id=pause.chat_id,
        message_id=PREVIEW_MESSAGE_ID,
    ):
        raise RuntimeError("approval preview could not be bound")
    common = {"telegram_user_id": user_id, "chat_id": pause.chat_id,
              "message_id": PREVIEW_MESSAGE_ID}
    if case.approval == "reject":
        if approvals.reject_approval(pause.token, **common) is not None:
            chat.discard_checkpoint(pause.token)
        return reply
    lease = memory.try_reserve_turn(user_id, kind="chat")
    if lease is None:
        raise RuntimeError("could not reserve the approval worker lease")
    if not chat.has_checkpoint(pause.token, user_id) or approvals.claim_approval(
        pause.token, **common
    ) is None:
        memory.release_turn(lease)
        raise RuntimeError("approval could not be claimed")
    return chat.resume_approved(lease, pause.token, provider=provider)


def _with_budget(case: EvalCase, run: Callable[[], Any]) -> Any:
    """Apply the case budget through the same Settings hook the runtime reads."""
    previous = chat.Settings

    def scoped_settings() -> Settings:
        # Compose with whatever Settings hook is active (tests patch it) instead of
        # re-reading .env from scratch.
        return previous().model_copy(
            update={"max_steps": case.budget.max_steps, "max_seconds": case.budget.max_seconds}
        )

    chat.Settings = scoped_settings  # type: ignore[assignment]
    try:
        return run()
    finally:
        chat.Settings = previous  # type: ignore[assignment]


def run_case(case: EvalCase, index: int, provider_mode: str) -> CaseResult:
    user_id = BASE_USER_ID + index
    result = CaseResult(case=case, user_id=user_id)
    offline = provider_mode == "mock"

    def restore() -> None:  # replaced once faults are installed
        return None

    started = time.monotonic()
    try:
        _installed, restore = install_faults(
            [(f.tool, list(f.script)) for f in case.faults], stub_texts=case.stubs,
            offline=offline,
        )
        reminder_id = _seed(user_id, case.id, case)
        script = _substitute(copy.deepcopy(case.mock_script), reminder_id)
        provider = MockLLM(script=script or None) if offline else get_provider()
        text = _substitute(case.input.text, reminder_id)
        lease = memory.admit_chat_turn(user_id)
        if lease is None:
            raise RuntimeError("user turn could not be admitted")
        attachments = {k: Path(v) for k, v in case.input.attachments.items()} or None
        reply = _with_budget(case, lambda: chat._run_reserved_message(
            lease, user_id, case.id, text, provider=provider,
            transport=case.input.transport, is_private_chat=True,
            attachments=attachments, chat_id=user_id,
        ))
        final = _with_budget(case, lambda: _decide_approval(case, user_id, reply, provider))
        result.reply = final.text
        # The resumed turn carries the whole-turn usage; the paused reply only the first half.
        result.usage = dict(final.usage or reply.usage)
    except Exception as exc:  # harness must survive any case
        message = redact_text(str(exc)[:200], secrets=configured_secrets(chat.Settings()))
        result.harness_error = f"{type(exc).__name__}: {message}"
    finally:
        restore()
        chat.interrupt_paused_turns()
        result.duration_ms = int((time.monotonic() - started) * 1000)
    facts = _facts(user_id, result.reply)
    result.task_id, result.stop_reason = facts["task_id"], facts["stop_reason"]
    result.checks = evaluate(case, facts, harness_error=result.harness_error)
    result.failure_group = failure_group(result.checks, result.stop_reason)
    return result


def model_name(provider_mode: str) -> str:
    if provider_mode == "mock":
        return "mock"
    return resolve_provider_config(Settings().llm_provider)[2]


def run_suite(cases: list[EvalCase], *, provider_mode: str, run_dir: Path, run_id: str
              ) -> SuiteResult:
    """Run every case on a fresh database under ``run_dir``; the engine stays bound after.

    A run id is never reused: an existing database would make user ids collide and
    the oracle counts drift, so the caller gets ``FileExistsError`` instead.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    database = run_dir / "laplace.db"
    if database.exists():
        raise FileExistsError(f"run already exists: {database}")
    reset_engine_for_tests(f"sqlite:///{database}")
    init_db()
    memory.start_chat_admission()
    suite = SuiteResult(run_id, provider_mode, model_name(provider_mode), datetime.now(UTC))
    for index, case in enumerate(cases):
        suite.results.append(run_case(case, index, provider_mode))
    suite.finished_at = datetime.now(UTC)
    return suite

