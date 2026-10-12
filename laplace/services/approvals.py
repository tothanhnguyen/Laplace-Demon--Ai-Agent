"""Durable approval lifecycle; transport and checkpoint ownership live elsewhere."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from laplace import repo
from laplace.config import Settings
from laplace.db import session_scope
from laplace.models import Conversation, Message, Task, ToolApproval, User
from laplace.tools.base import (
    ApprovalRequired,
    PreparedProposal,
    RiskLevel,
    get_tool,
    public_args_hash,
)

POLICY_VERSION = 1
MAX_PREVIEW_UTF16_UNITS = 4096
EXPIRY_BATCH_SIZE = 25
_ADMIN_STATUSES = frozenset({"invalidated", "failed", "cancelled", "interrupted"})
_ACTIVE_STATUSES = ("pending", "approved")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: int
    token: str
    user_id: int
    telegram_user_id: int
    task_id: int
    step_index: int
    tool_name: str
    risk_level: str
    params_json: str
    public_args_hash: str
    resource_fingerprint: str | None
    preview_json: str
    status: str
    created_at: int
    expires_at: int
    decided_at: int | None
    consumed_at: int | None
    chat_id: int
    preview_message_id: int | None
    result_json: str | None


@dataclass(frozen=True, slots=True)
class ApprovalPause:
    approval_id: int
    token: str
    telegram_user_id: int
    task_id: int
    expires_at: int
    chat_id: int
    preview: str


def epoch_now() -> int:
    return int(datetime.now(UTC).timestamp())


def _now(now_utc: int | None) -> int:
    value = epoch_now() if now_utc is None else now_utc
    if type(value) is not int or value < 0:
        raise ValueError("Approval time must be a nonnegative UTC epoch integer.")
    return value


def _snapshot(session: Session, row: ToolApproval) -> ApprovalRecord:
    owner = session.get(User, row.user_id)
    if owner is None:
        raise RuntimeError("Approval has no durable owner.")
    return ApprovalRecord(
        id=row.id,
        token=row.token,
        user_id=row.user_id,
        telegram_user_id=owner.telegram_user_id,
        task_id=row.task_id,
        step_index=row.step_index,
        tool_name=row.tool_name,
        risk_level=row.risk_level,
        params_json=row.params_json,
        public_args_hash=row.public_args_hash,
        resource_fingerprint=row.resource_fingerprint,
        preview_json=row.preview_json,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
        chat_id=row.chat_id,
        preview_message_id=row.preview_message_id,
        result_json=row.result_json,
    )


def _is_conflict(exc: Exception) -> bool:
    if isinstance(exc, repo.ApprovalTransitionConflict):
        return True
    original = getattr(exc, "orig", None)
    if isinstance(exc, OperationalError):
        code = getattr(original, "sqlite_errorcode", None)
        return code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    if isinstance(exc, IntegrityError):
        # Only approval generation/active uniqueness is an expected contention.
        return (
            getattr(original, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
            and str(original) in {
                "UNIQUE constraint failed: tool_approvals.token",
                "UNIQUE constraint failed: tool_approvals.user_id",
                "UNIQUE constraint failed: tool_approvals.task_id",
            }
        )
    return False


def _policy_current(row: ToolApproval) -> bool:
    try:
        preview = json.loads(row.preview_json)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(preview, dict)
        and type(preview.get("policy_version")) is int
        and preview["policy_version"] == POLICY_VERSION
    )


def _eligible(
    session: Session,
    row: ToolApproval | None,
    *,
    telegram_user_id: int,
    chat_id: int,
    now: int,
    message_id: int | None = None,
) -> bool:
    if (
        row is None
        or type(telegram_user_id) is not int
        or type(chat_id) is not int
        or telegram_user_id <= 0
        or chat_id != telegram_user_id
        or row.chat_id != chat_id
        or row.status != "pending"
        or row.expires_at <= now
        or not _policy_current(row)
    ):
        return False
    if message_id is not None and (
        type(message_id) is not int
        or message_id <= 0
        or row.preview_message_id != message_id
    ):
        return False
    task = session.get(Task, row.task_id)
    return task is not None and task.user_id == row.user_id and task.status == "awaiting_approval"


def create_pending(
    proposal: ApprovalRequired, *, chat_id: int, now_utc: int | None = None
) -> ApprovalPause | None:
    """Persist the complete frozen proposal or reject it without publishing a pause."""
    now = _now(now_utc)
    if not isinstance(proposal, ApprovalRequired) or not isinstance(proposal.tool_name, str):
        raise ValueError("A prepared approval proposal is required.")
    spec = get_tool(proposal.tool_name)
    if (
        spec is None
        or not isinstance(proposal.risk_level, RiskLevel)
        or not isinstance(proposal.proposal, PreparedProposal)
        or not isinstance(proposal.proposal.payload, BaseModel)
        or proposal.risk_level not in {RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND}
        or spec.risk_level != proposal.risk_level
        or spec.prepare is None
        or any(type(value) is not int or value <= 0 for value in (
            proposal.user_id, proposal.conversation_id,
            proposal.request_message_id, proposal.task_id,
        ))
        or type(proposal.step_index) is not int
        or proposal.step_index < 0
        or type(chat_id) is not int
        or chat_id <= 0
    ):
        raise ValueError("Approval proposal has invalid policy or request scope.")
    # Validate again at the persistence boundary, including constructed models.
    args = spec.args_model.model_validate_json(proposal.public_args_json, strict=True)
    payload = proposal.proposal.payload
    if not payload.model_config.get("frozen"):
        raise ValueError("Approval payload must be frozen.")
    payload = type(payload).model_validate(payload.model_dump(mode="python"), strict=True)
    facts = proposal.proposal.preview
    if not isinstance(facts, tuple) or not facts or any(
        not isinstance(fact, str) or not fact.strip() for fact in facts
    ):
        raise ValueError("Approval requires complete material preview facts.")
    fingerprint = getattr(payload, "resource_fingerprint", None)
    if fingerprint is not None and (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise ValueError("Approval resource fingerprint is invalid.")
    ttl = Settings().approval_timeout_seconds
    if type(ttl) is not int or ttl <= 0:
        raise ValueError("Approval timeout must be a positive finite integer.")
    expires = now + ttl
    expiry = datetime.fromtimestamp(expires, UTC).isoformat().replace("+00:00", "Z")
    preview = "\n".join((
        "Approval required",
        f"Tool: {proposal.tool_name}",
        f"Risk: {proposal.risk_level.value}",
        *facts,
        f"Expires: {expiry}",
    ))
    try:
        preview_units = len(preview.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError("Approval preview contains invalid Unicode.") from exc
    if preview_units > MAX_PREVIEW_UTF16_UNITS:
        raise ValueError("Complete approval preview exceeds Telegram's 4096 UTF-16 unit limit.")
    preview_json = json.dumps(
        {"policy_version": POLICY_VERSION, "facts": facts, "preview": preview},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    params_json = payload.model_dump_json()
    token = secrets.token_urlsafe(16)
    try:
        with session_scope() as session:
            owner = session.get(User, proposal.user_id)
            task = session.get(Task, proposal.task_id)
            conversation = session.get(Conversation, proposal.conversation_id)
            request = session.get(Message, proposal.request_message_id)
            if (
                owner is None
                or owner.telegram_user_id != chat_id
                or task is None
                or task.user_id != owner.id
                or task.conversation_id != proposal.conversation_id
                or task.request_message_id != proposal.request_message_id
                or conversation is None
                or conversation.user_id != owner.id
                or request is None
                or request.conversation_id != conversation.id
                or request.role != "user"
                or getattr(payload, "user_id", owner.id) != owner.id
                or getattr(payload, "destination_telegram_user_id", chat_id) != chat_id
            ):
                raise ValueError(
                    "Approval proposal does not match its durable private owner/request."
                )
            row = repo.create_tool_approval(
                session, token=token, user_id=owner.id, task_id=task.id,
                step_index=proposal.step_index, tool_name=proposal.tool_name,
                risk_level=proposal.risk_level.value, params_json=params_json,
                public_args_hash=public_args_hash(args), resource_fingerprint=fingerprint,
                preview_json=preview_json, created_at=now, expires_at=expires, chat_id=chat_id,
            )
            pause = ApprovalPause(
                row.id, token, owner.telegram_user_id, task.id, expires, chat_id, preview
            )
        return pause
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return None


def get_approval(token: str, *, telegram_user_id: int | None = None) -> ApprovalRecord | None:
    with session_scope() as session:
        row = repo.find_tool_approval(session, token, telegram_user_id=telegram_user_id)
        return _snapshot(session, row) if row is not None else None


def bind_preview(
    token: str, *, telegram_user_id: int, chat_id: int, message_id: int,
    now_utc: int | None = None,
) -> bool:
    now = _now(now_utc)
    if type(message_id) is not int or message_id <= 0:
        return False
    try:
        with session_scope() as session:
            row = repo.find_tool_approval(session, token, telegram_user_id=telegram_user_id)
            if not _eligible(
                session, row, telegram_user_id=telegram_user_id, chat_id=chat_id, now=now
            ):
                return False
            return repo.bind_approval_preview(
                session, row.id, chat_id=chat_id, message_id=message_id, now_utc=now,
            )
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return False


def _decide(
    token: str, *, telegram_user_id: int, chat_id: int, message_id: int,
    now_utc: int | None, reject: bool,
) -> ApprovalRecord | None:
    now = _now(now_utc)
    if type(message_id) is not int or message_id <= 0:
        return None
    try:
        with session_scope() as session:
            row = repo.find_tool_approval(session, token, telegram_user_id=telegram_user_id)
            if not _eligible(
                session, row, telegram_user_id=telegram_user_id,
                chat_id=chat_id, message_id=message_id, now=now,
            ):
                return None
            if reject:
                winner = repo.resolve_tool_approval(
                    session, token, status="rejected", now_utc=now,
                    telegram_user_id=telegram_user_id,
                )
            else:
                winner = repo.claim_tool_approval(
                    session, token, telegram_user_id=telegram_user_id,
                    chat_id=chat_id, message_id=message_id, now_utc=now,
                )
            return _snapshot(session, winner) if winner is not None else None
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return None


def claim_approval(
    token: str, *, telegram_user_id: int, chat_id: int, message_id: int,
    now_utc: int | None = None,
) -> ApprovalRecord | None:
    return _decide(
        token, telegram_user_id=telegram_user_id, chat_id=chat_id,
        message_id=message_id, now_utc=now_utc, reject=False,
    )


def reject_approval(
    token: str, *, telegram_user_id: int, chat_id: int, message_id: int,
    now_utc: int | None = None,
) -> ApprovalRecord | None:
    return _decide(
        token, telegram_user_id=telegram_user_id, chat_id=chat_id,
        message_id=message_id, now_utc=now_utc, reject=True,
    )


def terminate_approval(
    token: str, *, status: str, telegram_user_id: int | None = None,
    allowed_statuses: tuple[str, ...] = ("pending",), now_utc: int | None = None,
) -> ApprovalRecord | None:
    if status not in _ADMIN_STATUSES or not allowed_statuses or any(
        source not in _ACTIVE_STATUSES for source in allowed_statuses
    ):
        raise ValueError("Unsupported administrative approval transition.")
    now = _now(now_utc)
    try:
        with session_scope() as session:
            row = repo.resolve_tool_approval(
                session, token, status=status, telegram_user_id=telegram_user_id,
                allowed_statuses=allowed_statuses, now_utc=now,
            )
            return _snapshot(session, row) if row is not None else None
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return None


def cancel_pending_approval(
    telegram_user_id: int, *, now_utc: int | None = None,
) -> ApprovalRecord | None:
    now = _now(now_utc)
    try:
        with session_scope() as session:
            row = session.scalar(
                select(ToolApproval).join(User, User.id == ToolApproval.user_id).where(
                    User.telegram_user_id == telegram_user_id, ToolApproval.status == "pending",
                )
            )
            if row is None:
                return None
            winner = repo.resolve_tool_approval(
                session, row.token, status="cancelled",
                telegram_user_id=telegram_user_id, now_utc=now,
            )
            return _snapshot(session, winner) if winner is not None else None
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return None


def expire_approvals(*, now_utc: int | None = None) -> list[ApprovalRecord]:
    now = _now(now_utc)
    try:
        with session_scope() as session:
            expired = []
            for row in repo.expired_tool_approvals(session, now, limit=EXPIRY_BATCH_SIZE):
                winner = repo.resolve_tool_approval(
                    session, row.token, status="expired", now_utc=now
                )
                if winner is not None:
                    expired.append(_snapshot(session, winner))
            return expired
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        if not _is_conflict(exc):
            raise
        return []


def interrupt_approvals(*, now_utc: int | None = None) -> list[ApprovalRecord]:
    now = _now(now_utc)
    try:
        with session_scope() as session:
            return [_snapshot(session, row) for row in repo.interrupt_tool_approvals(session, now)]
    except (repo.ApprovalTransitionConflict, IntegrityError, OperationalError) as exc:
        # Startup/shutdown require a committed scan, not callback-style soft failure.
        # Do not surface SQL parameters (including approval tokens) in transport logs.
        raise RuntimeError(
            f"Approval interruption failed ({type(exc).__name__}); admission must stay disabled."
        ) from None
