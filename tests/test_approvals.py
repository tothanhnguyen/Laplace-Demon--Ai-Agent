"""Approval authority, durable transitions, immutable preview, and receipt boundaries."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from laplace import repo
from laplace.config import Settings
from laplace.db import get_engine, init_db, reset_engine_for_tests, session_scope
from laplace.models import Reminder, Task, ToolApproval
from laplace.services import approvals
from laplace.services.budget import TurnBudget
from laplace.tools.base import (
    ApprovalRequired,
    PreparedProposal,
    RiskLevel,
    ToolContext,
    ToolResult,
    execute,
    load_builtin_tools,
    public_args_hash,
)
from laplace.tools.reminders import CreateReminderArgs

NOW = 1_800_000_000
OWNER = 7001
MESSAGE_ID = 901


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path, monkeypatch):
    reset_engine_for_tests(f"sqlite:///{tmp_path / 'approvals.db'}")
    monkeypatch.setenv("LAPLACE_APPROVAL_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("LAPLACE_TIMEZONE", "Asia/Ho_Chi_Minh")
    monkeypatch.setattr(approvals, "epoch_now", lambda: NOW)
    load_builtin_tools()
    init_db()
    yield
    reset_engine_for_tests(None)


def _context(actor=OWNER, *, effect="create_reminder"):
    with session_scope() as session:
        user = repo.get_or_create_user(session, actor, None)
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(session, conversation, "user", f"/{effect} explicit request")
        task = repo.create_task(
            session, user_id=user.id, conversation_id=conversation.id,
            request_message_id=request.id, goal="Approval lifecycle exercise",
        )
        return ToolContext(
            budget=TurnBudget(Settings(_env_file=None, max_steps=20)),
            user_id=user.id, telegram_user_id=actor, transport="telegram", is_private_chat=True,
            conversation_id=conversation.id, request_message_id=request.id,
            task_id=task.id, step_index=2, now_utc=datetime.fromtimestamp(NOW, UTC),
            timezone="Asia/Ho_Chi_Minh", authorized_effects=frozenset({effect}),
        )


def _proposal(
    context=None, *, message="full approved message", effect="create_reminder", params=None
):
    context = context or _context(effect=effect)
    params = params or {"message": message, "delay_seconds": 3600}
    outcome = execute(effect, params, context)
    assert isinstance(outcome, ApprovalRequired)
    return context, outcome


def _pending(context=None, *, message="full approved message", actor=OWNER):
    context, proposal = _proposal(context or _context(actor), message=message)
    pause = approvals.create_pending(proposal, chat_id=actor, now_utc=NOW)
    assert pause is not None
    return context, proposal, pause


def _bind(pause, *, actor=OWNER):
    assert approvals.bind_preview(
        pause.token, telegram_user_id=actor, chat_id=actor,
        message_id=MESSAGE_ID, now_utc=NOW,
    )


def _claim(pause, *, actor=OWNER, now=NOW):
    return approvals.claim_approval(
        pause.token, telegram_user_id=actor, chat_id=actor,
        message_id=MESSAGE_ID, now_utc=now,
    )


def _state(token):
    with session_scope() as session:
        row = repo.find_tool_approval(session, token)
        task = session.get(Task, row.task_id)
        return row.status, task.status, row.decided_at, row.consumed_at, row.result_json


def _execute_approved(context, proposal, pause):
    _bind(pause, actor=context.telegram_user_id)
    claimed = _claim(pause, actor=context.telegram_user_id)
    assert claimed is not None
    result = execute(
        proposal.tool_name, json.loads(proposal.public_args_json),
        replace(context, approval_id=pause.approval_id, approval_token=pause.token),
    )
    assert isinstance(result, ToolResult) and result.ok
    return result


def test_pending_is_frozen_private_and_hashes_validated_public_json():
    text = 'Tiếng Việt 🧭\n"all material facts"'
    context, proposal, pause = _pending(message=text)
    row = approvals.get_approval(pause.token, telegram_user_id=OWNER)
    assert row is not None
    assert row.telegram_user_id == OWNER and row.task_id == context.task_id
    assert row.status == "pending" and row.expires_at == NOW + 90
    assert row.public_args_hash == public_args_hash(
        CreateReminderArgs.model_validate_json(proposal.public_args_json)
    )
    assert json.loads(row.params_json)["message"] == text
    assert row.params_json == proposal.proposal.payload.model_dump_json()
    assert all(fact in pause.preview for fact in proposal.proposal.preview)
    assert json.dumps(text, ensure_ascii=False) in pause.preview
    assert "Asia/Ho_Chi_Minh" in pause.preview
    due = datetime.fromtimestamp(NOW + 3600, UTC)
    assert due.isoformat().replace("+00:00", "Z") in pause.preview
    assert len(base64.urlsafe_b64decode(pause.token + "==")) == 16
    assert _state(pause.token) == ("pending", "awaiting_approval", None, None, None)
    with pytest.raises(FrozenInstanceError):
        pause.token = "replacement"
    with pytest.raises(FrozenInstanceError):
        row.status = "approved"
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Reminder)) == 0


def test_canonical_public_hash_ignores_json_order_and_whitespace():
    _, proposal = _proposal()
    alternate = replace(
        proposal, public_args_json=(
            ' { "delay_seconds" : 3600, "message": "full approved message", "run_at":null } '
        ),
    )
    pause = approvals.create_pending(alternate, chat_id=OWNER, now_utc=NOW)
    row = approvals.get_approval(pause.token)
    assert row.public_args_hash == public_args_hash(
        CreateReminderArgs.model_validate_json(proposal.public_args_json)
    )


@pytest.mark.parametrize("replacement", [
    {"public_args_json": '{"message":"x","delay_seconds":"3600"}'},
    {"public_args_json": '{"message":"x","delay_seconds":3600,"approval_id":1}'},
    {"public_args_json": "not-json"},
    {"risk_level": RiskLevel.READ},
    {"step_index": -1},
    {"user_id": 999999},
    {"conversation_id": 999999},
    {"request_message_id": 999999},
])
def test_malformed_proposal_has_no_durable_pause(replacement):
    context, proposal = _proposal()
    with pytest.raises(ValueError):
        approvals.create_pending(replace(proposal, **replacement), chat_id=OWNER, now_utc=NOW)
    with session_scope() as session:
        assert session.get(Task, context.task_id).status == "running"
        assert session.scalar(select(func.count()).select_from(ToolApproval)) == 0
        assert session.scalar(select(func.count()).select_from(Reminder)) == 0


def test_empty_or_oversized_preview_is_rejected_without_truncation():
    context, proposal = _proposal()
    for facts in ((), (" ",), ("🧭" * 2100,)):
        invalid = replace(
            proposal, proposal=PreparedProposal(payload=proposal.proposal.payload, preview=facts)
        )
        with pytest.raises(ValueError):
            approvals.create_pending(invalid, chat_id=OWNER, now_utc=NOW)
    with session_scope() as session:
        assert session.get(Task, context.task_id).status == "running"
        assert session.scalar(select(func.count()).select_from(ToolApproval)) == 0


@pytest.mark.parametrize("old,new,fit", [
    ("漢" * 1000, "字" * 1000, True),
    ("🧭" * 1000, "🚀" * 1000, False),
])
def test_update_preview_preserves_both_old_new_messages_and_utf16_budget(old, new, fit):
    context, proposal, pause = _pending(message=old)
    receipt = _execute_approved(context, proposal, pause)
    update_context, update_proposal = _proposal(
        effect="update_reminder", params={"reminder_id": receipt.data["id"], "message": new},
    )
    if fit:
        updated = approvals.create_pending(update_proposal, chat_id=OWNER, now_utc=NOW)
        assert updated is not None
        assert json.dumps(old, ensure_ascii=False) in updated.preview
        assert json.dumps(new, ensure_ascii=False) in updated.preview
        assert all(fact in updated.preview for fact in update_proposal.proposal.preview)
        assert len(updated.preview.encode("utf-16-le")) // 2 <= 4096
    else:
        with pytest.raises(ValueError, match="UTF-16"):
            approvals.create_pending(update_proposal, chat_id=OWNER, now_utc=NOW)
        with session_scope() as session:
            assert session.get(Task, update_context.task_id).status == "running"
            assert session.scalar(select(func.count()).select_from(ToolApproval)) == 1
    with session_scope() as session:
        assert session.get(Reminder, receipt.data["id"]).message == old


@pytest.mark.parametrize("method", [approvals.claim_approval, approvals.reject_approval])
@pytest.mark.parametrize("actor,chat,message,now", [
    (OWNER + 1, OWNER, MESSAGE_ID, NOW),
    (OWNER, OWNER + 1, MESSAGE_ID, NOW),
    (OWNER, OWNER, MESSAGE_ID + 1, NOW),
    (OWNER, OWNER, 0, NOW),
    (OWNER, OWNER, MESSAGE_ID, NOW + 90),
    (OWNER, OWNER, MESSAGE_ID, NOW + 91),
])
def test_callback_requires_owner_private_binding_and_strict_deadline(
    method, actor, chat, message, now
):
    _, _, pause = _pending()
    _bind(pause)
    assert method(
        pause.token, telegram_user_id=actor, chat_id=chat,
        message_id=message, now_utc=now,
    ) is None
    assert _state(pause.token) == ("pending", "awaiting_approval", None, None, None)


def test_binding_is_one_shot_private_owner_and_unexpired():
    _, _, pause = _pending()
    for actor, chat, message, now in (
        (OWNER + 1, OWNER + 1, MESSAGE_ID, NOW),
        (OWNER, OWNER + 1, MESSAGE_ID, NOW),
        (OWNER, OWNER, 0, NOW),
        (OWNER, OWNER, MESSAGE_ID, NOW + 90),
    ):
        assert not approvals.bind_preview(
            pause.token, telegram_user_id=actor, chat_id=chat,
            message_id=message, now_utc=now,
        )
    assert _claim(pause) is None
    _bind(pause)
    assert not approvals.bind_preview(
        pause.token, telegram_user_id=OWNER, chat_id=OWNER,
        message_id=MESSAGE_ID + 1, now_utc=NOW,
    )
    assert approvals.get_approval(pause.token).preview_message_id == MESSAGE_ID


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_policy_version_mismatch_never_claims_or_binds(version):
    _, _, pause = _pending()
    _bind(pause)
    with session_scope() as session:
        session.execute(update(ToolApproval).where(ToolApproval.id == pause.approval_id).values(
            preview_json=json.dumps({"policy_version": version}),
        ))
    assert _claim(pause) is None
    assert approvals.reject_approval(
        pause.token, telegram_user_id=OWNER, chat_id=OWNER,
        message_id=MESSAGE_ID, now_utc=NOW,
    ) is None
    with session_scope() as session:
        session.execute(update(ToolApproval).where(ToolApproval.id == pause.approval_id).values(
            preview_message_id=None,
        ))
    assert not approvals.bind_preview(
        pause.token, telegram_user_id=OWNER, chat_id=OWNER, message_id=MESSAGE_ID, now_utc=NOW,
    )
    assert _state(pause.token)[0:2] == ("pending", "awaiting_approval")


def test_approve_reject_concurrent_cas_has_one_durable_winner():
    _, _, pause = _pending()
    _bind(pause)
    start = Barrier(2)

    def decide(method):
        start.wait(timeout=5)
        return method(
            pause.token, telegram_user_id=OWNER, chat_id=OWNER,
            message_id=MESSAGE_ID, now_utc=NOW + 1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, [approvals.claim_approval, approvals.reject_approval]))
    winners = [result for result in outcomes if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert _state(pause.token) == (
        winner.status, "running" if winner.status == "approved" else "rejected",
        NOW + 1, None, None,
    )
    assert _claim(pause) is None
    assert approvals.reject_approval(
        pause.token, telegram_user_id=OWNER, chat_id=OWNER,
        message_id=MESSAGE_ID, now_utc=NOW + 2,
    ) is None
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Reminder)) == 0


def test_paired_transition_conflict_rolls_back_approval_and_task(monkeypatch):
    _, _, pause = _pending()
    _bind(pause)
    original = repo.claim_tool_approval

    def fail_after_claim(session, *args, **kwargs):
        assert original(session, *args, **kwargs) is not None
        raise repo.ApprovalTransitionConflict("injected paired transition failure")

    monkeypatch.setattr(repo, "claim_tool_approval", fail_after_claim)
    assert _claim(pause) is None
    assert _state(pause.token) == ("pending", "awaiting_approval", None, None, None)


def test_unique_active_owner_task_conflict_rolls_back_new_task_transition():
    _, proposal, pause = _pending()
    assert approvals.create_pending(proposal, chat_id=OWNER, now_utc=NOW) is None
    other_context, other_proposal = _proposal()
    assert approvals.create_pending(other_proposal, chat_id=OWNER, now_utc=NOW) is None
    _bind(pause)
    assert _claim(pause) is not None
    assert approvals.create_pending(other_proposal, chat_id=OWNER, now_utc=NOW) is None
    with session_scope() as session:
        assert session.get(Task, other_context.task_id).status == "running"
        assert session.scalar(select(func.count()).select_from(ToolApproval)) == 1


@pytest.mark.parametrize("status,task_status", [
    ("invalidated", "failed"), ("failed", "failed"),
    ("cancelled", "cancelled"), ("interrupted", "interrupted"),
])
@pytest.mark.parametrize("claimed", [False, True])
def test_administration_transitions_only_allowed_active_sources(status, task_status, claimed):
    _, _, pause = _pending()
    if claimed:
        _bind(pause)
        assert _claim(pause) is not None
        assert approvals.terminate_approval(pause.token, status=status, now_utc=NOW + 1) is None
    assert approvals.terminate_approval(
        pause.token, status=status, telegram_user_id=OWNER + 1,
        allowed_statuses=("pending", "approved"), now_utc=NOW + 1,
    ) is None
    winner = approvals.terminate_approval(
        pause.token, status=status, telegram_user_id=OWNER,
        allowed_statuses=("approved",) if claimed else ("pending",), now_utc=NOW + 1,
    )
    assert winner is not None and winner.status == status
    assert _state(pause.token)[0:2] == (status, task_status)
    assert approvals.terminate_approval(
        pause.token, status="failed", allowed_statuses=("pending", "approved"), now_utc=NOW + 2,
    ) is None


def test_cancel_pending_requires_owner_but_not_preview_binding():
    _, _, pause = _pending()
    assert approvals.cancel_pending_approval(OWNER + 1, now_utc=NOW) is None
    cancelled = approvals.cancel_pending_approval(OWNER, now_utc=NOW)
    assert cancelled is not None and cancelled.token == pause.token
    assert cancelled.preview_message_id is None
    assert _state(pause.token)[0:2] == ("cancelled", "cancelled")
    assert approvals.cancel_pending_approval(OWNER, now_utc=NOW) is None


def test_expiry_is_bounded_and_exact_deadline_does_not_require_cron():
    pauses = [_pending(actor=OWNER + index)[2] for index in range(27)]
    _bind(pauses[0])
    assert approvals.expire_approvals(now_utc=NOW + 89) == []
    assert _claim(pauses[0], now=NOW + 90) is None
    first = approvals.expire_approvals(now_utc=NOW + 90)
    assert len(first) == 25 and len({row.token for row in first}) == 25
    assert all(row.status == "expired" for row in first)
    second = approvals.expire_approvals(now_utc=NOW + 90)
    assert len(second) == 2
    assert {row.token for row in first + second} == {pause.token for pause in pauses}
    assert all(_state(pause.token)[0:2] == ("expired", "expired") for pause in pauses)
    assert approvals.expire_approvals(now_utc=NOW + 90) == []


def test_unexpected_database_fault_surfaces_and_rolls_back(monkeypatch):
    context, proposal = _proposal()
    original = repo.create_tool_approval

    def fail_after_insert(session, **kwargs):
        original(session, **kwargs)
        raise OperationalError("write receipt", {}, RuntimeError("disk failure"))

    monkeypatch.setattr(repo, "create_tool_approval", fail_after_insert)
    with pytest.raises(OperationalError):
        approvals.create_pending(proposal, chat_id=OWNER, now_utc=NOW)
    with session_scope() as session:
        assert session.get(Task, context.task_id).status == "running"
        assert session.scalar(select(func.count()).select_from(ToolApproval)) == 0


def test_restart_interrupts_unconsumed_rows_but_retains_real_canonical_receipt():
    executed_context, executed_proposal, executed_pause = _pending()
    result = _execute_approved(executed_context, executed_proposal, executed_pause)
    durable = approvals.get_approval(executed_pause.token)
    assert durable.status == "executed"
    assert ToolResult.model_validate_json(durable.result_json) == result
    _, _, pending = _pending(actor=OWNER + 1)
    _, _, approved = _pending(actor=OWNER + 2)
    _bind(approved, actor=OWNER + 2)
    assert _claim(approved, actor=OWNER + 2) is not None
    assert approvals.terminate_approval(
        executed_pause.token, status="failed", allowed_statuses=("approved",), now_utc=NOW + 1,
    ) is None
    # Reopen the real file DB: startup cannot depend on detached ORM objects.
    database_url = str(get_engine().url)
    reset_engine_for_tests(database_url)
    init_db()
    assert approvals.get_approval(executed_pause.token).result_json == durable.result_json
    interrupted = approvals.interrupt_approvals(now_utc=NOW + 2)
    assert {row.token for row in interrupted} == {
        pending.token, approved.token, executed_pause.token
    }
    assert _state(pending.token)[0:2] == ("interrupted", "interrupted")
    assert _state(approved.token)[0:2] == ("interrupted", "interrupted")
    assert _state(executed_pause.token) == (
        "executed", "interrupted", NOW, NOW, durable.result_json,
    )
    assert approvals.interrupt_approvals(now_utc=NOW + 3) == []
    after = approvals.get_approval(executed_pause.token)
    assert after.result_json == durable.result_json and after.consumed_at == NOW
    replay = execute(
        executed_proposal.tool_name, json.loads(executed_proposal.public_args_json),
        replace(
            executed_context, approval_id=executed_pause.approval_id,
            approval_token=executed_pause.token,
        ),
    )
    assert isinstance(replay, ToolResult) and not replay.ok
    with session_scope() as session:
        reminders = session.scalars(select(Reminder)).all()
        assert [(row.id, row.message, row.status) for row in reminders] == [
            (result.data["id"], "full approved message", "pending"),
        ]


def test_approval_can_be_claimed_one_second_before_deadline():
    _, _, pause = _pending()
    _bind(pause)
    claimed = _claim(pause, now=NOW + 89)
    assert claimed is not None and claimed.status == "approved"
    assert _state(pause.token) == ("approved", "running", NOW + 89, None, None)
    assert approvals.expire_approvals(now_utc=NOW + 90) == []
    assert approvals.cancel_pending_approval(OWNER, now_utc=NOW + 90) is None


def test_cancel_preview_contains_full_existing_material_and_never_cancels_by_itself():
    message = 'Keep 🧭 every line\nand "quoted" text'
    context, proposal, pause = _pending(message=message)
    receipt = _execute_approved(context, proposal, pause)
    _, cancellation = _proposal(
        effect="cancel_reminder", params={"reminder_id": receipt.data["id"]},
    )
    cancelled_pause = approvals.create_pending(cancellation, chat_id=OWNER, now_utc=NOW)
    assert cancelled_pause is not None
    assert all(fact in cancelled_pause.preview for fact in cancellation.proposal.preview)
    assert json.dumps(message, ensure_ascii=False) in cancelled_pause.preview
    assert str(receipt.data["id"]) in cancelled_pause.preview
    record = approvals.get_approval(cancelled_pause.token)
    payload = json.loads(record.params_json)
    assert payload["operation"] == "cancel" and payload["message"] == message
    with session_scope() as session:
        reminder = session.get(Reminder, receipt.data["id"])
        assert reminder.status == "pending" and reminder.message == message
