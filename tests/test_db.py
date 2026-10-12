"""Kiểm thử tầng lưu trữ: models, engine in-memory và các helper repo."""

import json
import sqlite3

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from laplace import repo
from laplace.db import get_engine, init_db, reset_engine_for_tests, session_scope
from laplace.models import Conversation, LLMCall, Message, Task, ToolApproval, ToolCall, User
from laplace.repo import (
    add_message,
    get_or_create_conversation,
    get_or_create_user,
    recent_messages,
    record_llm_call,
    record_trace,
    user_usage,
)


@pytest.fixture()
def db() -> None:
    """Chuyển engine sang SQLite in-memory và tạo schema mới cho mỗi test."""
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_tests(None)


def test_user_conversation_message_roundtrip(db: None) -> None:
    with session_scope() as session:
        user = get_or_create_user(session, 111, "alice")
        conversation = get_or_create_conversation(session, user)
        add_message(session, conversation, "user", "xin chào")
        add_message(session, conversation, "assistant", "chào bạn")

    with session_scope() as session:
        user = get_or_create_user(session, 111, "alice")
        conversation = get_or_create_conversation(session, user)
        messages = recent_messages(session, conversation)

        assert user.username == "alice"
        assert [m.role for m in messages] == ["user", "assistant"]
        assert [m.content for m in messages] == ["xin chào", "chào bạn"]


def test_two_users_do_not_share_data(db: None) -> None:
    with session_scope() as session:
        alice = get_or_create_user(session, 111, "alice")
        bob = get_or_create_user(session, 222, "bob")
        conv_a = get_or_create_conversation(session, alice)
        conv_b = get_or_create_conversation(session, bob)
        add_message(session, conv_a, "user", "của alice")
        add_message(session, conv_b, "user", "của bob")

        assert alice.id != bob.id
        assert conv_a.id != conv_b.id
        assert [m.content for m in recent_messages(session, conv_a)] == ["của alice"]
        assert [m.content for m in recent_messages(session, conv_b)] == ["của bob"]


def test_recent_messages_respects_limit_and_order(db: None) -> None:
    with session_scope() as session:
        user = get_or_create_user(session, 111, None)
        conversation = get_or_create_conversation(session, user)
        for i in range(15):
            add_message(session, conversation, "user", f"msg {i}")

        messages = recent_messages(session, conversation, limit=10)

        assert [m.content for m in messages] == [f"msg {i}" for i in range(5, 15)]


def test_user_usage_aggregates_and_empty_group_is_zero(db: None) -> None:
    with session_scope() as session:
        alice = get_or_create_user(session, 111, "alice")
        bob = get_or_create_user(session, 222, "bob")
        record_llm_call(
            session,
            user_id=alice.id,
            purpose="answer",
            provider="mock",
            model="mock-1",
            prompt_tokens=100,
            completion_tokens=40,
            cost_usd=0.001,
            latency_ms=5,
        )
        record_llm_call(
            session,
            user_id=alice.id,
            purpose="classify",
            provider="mock",
            model="mock-1",
            prompt_tokens=50,
            completion_tokens=10,
            cost_usd=0.0005,
            latency_ms=3,
        )

        usage = user_usage(session, alice)
        assert usage == {
            "llm_calls": 2,
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "total_tokens": 200,
            "cost_usd": 0.0015,
            "known_cost_usd": 0.0015,
            "budget_charge_usd": 0.0015,
            "unknown_attempts": 0,
            "physical_attempts": 2,
        }

        empty = user_usage(session, bob)
        assert empty == {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "known_cost_usd": 0.0,
            "budget_charge_usd": 0.0,
            "unknown_attempts": 0,
            "physical_attempts": 0,
        }


def test_record_trace_persists_only_allowlisted_audit_metadata(db: None) -> None:
    payload = {
        "event": "tool_call",
        "tool": "web_search",
        "tool_call_id": 7,
        "status": "completed",
        "latency_ms": 3,
        "note": "tiếng Việt must stay private",
    }
    original = dict(payload)
    with session_scope() as session:
        user = get_or_create_user(session, 111, None)
        conversation = get_or_create_conversation(session, user)
        trace = record_trace(
            session, conversation_id=conversation.id, step_index=0, payload=payload
        )

        assert trace.task_id is None
        assert json.loads(trace.payload_json) == {
            "event": "tool_call",
            "tool_name": "web_search",
            "tool_call_id": 7,
            "status": "completed",
            "step_index": 0,
            "latency_ms": 3,
        }
        assert "tiếng Việt" not in trace.payload_json
        assert payload == original


def test_sprint3_database_upgrade_is_additive_and_idempotent(tmp_path) -> None:
    database = tmp_path / "sprint3.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_user_id INTEGER UNIQUE,
            username VARCHAR(64),
            created_at DATETIME
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title VARCHAR(200),
            created_at DATETIME
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            conversation_id INTEGER REFERENCES conversations(id),
            goal TEXT NOT NULL,
            status VARCHAR(32),
            current_step INTEGER,
            total_steps INTEGER,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE tool_calls (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id),
            tool_name VARCHAR(64),
            params_json TEXT,
            result_json TEXT,
            ok BOOLEAN,
            error TEXT,
            latency_ms INTEGER,
            created_at DATETIME
        );
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id),
            user_id INTEGER REFERENCES users(id),
            purpose VARCHAR(32),
            provider VARCHAR(32),
            model VARCHAR(64),
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd FLOAT,
            latency_ms INTEGER,
            created_at DATETIME
        );
        INSERT INTO users(id, telegram_user_id, username)
        VALUES (1, 9001, 'legacy');
        INSERT INTO conversations(id, user_id, title)
        VALUES (1, 1, 'legacy conversation');
        INSERT INTO tasks(id, user_id, conversation_id, goal, status)
        VALUES (1, 1, 1, 'legacy task', 'completed');
        INSERT INTO tool_calls(id, task_id, tool_name, params_json, result_json, ok, latency_ms)
        VALUES (1, 1, 'legacy', '{}', '{}', 1, 0), (2, 1, 'legacy', '{}', '{}', 1, 0);
        INSERT INTO llm_calls(
            id, task_id, user_id, purpose, provider, model,
            prompt_tokens, completion_tokens, cost_usd, latency_ms
        ) VALUES
            (1, 1, NULL, 'agent', 'legacy', 'priced', 100, 25, 0.02, 10),
            (2, NULL, 1, 'compression', 'legacy', 'priced', 80, 10, 0.01, 8);
        """
    )
    connection.commit()
    connection.close()

    reset_engine_for_tests(f"sqlite:///{database}")
    try:
        init_db()
        init_db()
        schema = inspect(get_engine())
        reminder_columns = {item["name"] for item in schema.get_columns("reminders")}
        reminder_indexes = {item["name"] for item in schema.get_indexes("reminders")}
        assert {
            "due_at_utc",
            "next_attempt_at_utc",
            "attempt_count",
            "first_attempt_at_utc",
            "last_attempt_at_utc",
            "sent_at_utc",
            "telegram_message_id",
        } <= reminder_columns
        assert {
            "ix_reminders_due",
            "ix_reminders_user_status_due",
        } <= reminder_indexes
        assert "approval_id" in {
            item["name"] for item in schema.get_columns("tool_calls")
        }
        assert "uq_tool_calls_approval_id" in {
            item["name"] for item in schema.get_indexes("tool_calls")
        }
        assert {
            "uq_tool_approvals_active_user",
            "uq_tool_approvals_active_task",
            "ix_tool_approvals_expiry",
            "ix_tool_approvals_user_status",
        } <= {item["name"] for item in schema.get_indexes("tool_approvals")}
        assert {
            "request_message_id", "call_group_id", "attempt_index",
            "accounting_status", "budget_charge_usd", "accounting_detail",
        } <= {item["name"] for item in schema.get_columns("llm_calls")}
        assert "uq_llm_calls_group_attempt" in {
            item["name"] for item in schema.get_indexes("llm_calls")
        }
        with get_engine().connect() as upgraded:
            assert upgraded.execute(text("PRAGMA foreign_key_check")).all() == []
            assert upgraded.execute(
                text("SELECT telegram_user_id, username FROM users WHERE id=1")
            ).one() == (9001, "legacy")
            assert upgraded.execute(text("SELECT goal, status FROM tasks WHERE id=1")).one() == (
                "legacy task",
                "completed",
            )
            assert upgraded.execute(
                text("SELECT id, approval_id FROM tool_calls ORDER BY id")
            ).all() == [(1, None), (2, None)]
            assert upgraded.execute(
                text(
                    "SELECT id, prompt_tokens, completion_tokens, cost_usd, "
                    "call_group_id, accounting_status, budget_charge_usd "
                    "FROM llm_calls ORDER BY id"
                )
            ).all() == [
                (1, 100, 25, 0.02, None, None, None),
                (2, 80, 10, 0.01, None, None, None),
            ]
        with session_scope() as session:
            usage = user_usage(session, session.get(User, 1))
            assert usage == {
                "llm_calls": 2,
                "physical_attempts": 2,
                "prompt_tokens": 180,
                "completion_tokens": 35,
                "total_tokens": 215,
                "cost_usd": 0.03,
                "known_cost_usd": 0.03,
                "budget_charge_usd": 0.03,
                "unknown_attempts": 0,
            }
            assert repo.total_usage(session) == usage
        with pytest.raises(IntegrityError), get_engine().begin() as upgraded:
            upgraded.execute(text("UPDATE tool_calls SET approval_id=999 WHERE id=1"))
        with pytest.raises(IntegrityError), get_engine().begin() as upgraded:
            upgraded.execute(text("UPDATE llm_calls SET request_message_id=999 WHERE id=1"))
        with session_scope() as session:
            _attempt(session, group="migrated-physical-call", user_id=1)
        with pytest.raises(IntegrityError), session_scope() as session:
            _attempt(session, group="migrated-physical-call", user_id=1)
        with session_scope() as session:
            usage = repo.total_usage(session)
            assert usage["physical_attempts"] == usage["llm_calls"] == 3
            assert usage["cost_usd"] == 0.03 and usage["budget_charge_usd"] == 0.07
            assert usage["unknown_attempts"] == 1
    finally:
        reset_engine_for_tests()


def _approval_task(telegram_id: int) -> tuple[int, int]:
    with session_scope() as session:
        owner = repo.get_or_create_user(session, telegram_id, None)
        conversation = repo.get_or_create_conversation(session, owner)
        message = repo.add_message(session, conversation, "user", "remind me")
        task = repo.create_task(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            request_message_id=message.id,
            goal="reminder",
        )
        return owner.id, task.id


def _pending_approval(session, user_id: int, task_id: int, token: str, chat_id: int):
    return repo.create_tool_approval(
        session,
        token=token,
        user_id=user_id,
        task_id=task_id,
        step_index=1,
        tool_name="create_reminder",
        risk_level="EXTERNAL_SEND",
        params_json="{}",
        public_args_hash="a" * 64,
        resource_fingerprint=None,
        preview_json='{"policy_version":1}',
        created_at=100,
        expires_at=200,
        chat_id=chat_id,
    )


def test_approval_binding_claim_owner_deadline_and_replay(db: None) -> None:
    owner, task_id = _approval_task(901)
    with session_scope() as session:
        approval = _pending_approval(session, owner, task_id, "claim-generation", 901)
        approval_id = approval.id
        assert session.get(Task, task_id).status == "awaiting_approval"
        assert repo.find_tool_approval(session, approval.token, telegram_user_id=902) is None
        assert not repo.bind_approval_preview(
            session, approval_id, chat_id=902, message_id=5, now_utc=150
        )
        assert not repo.bind_approval_preview(
            session, approval_id, chat_id=901, message_id=5, now_utc=200
        )
        assert repo.bind_approval_preview(
            session, approval_id, chat_id=901, message_id=5, now_utc=150
        )
        assert not repo.bind_approval_preview(
            session, approval_id, chat_id=901, message_id=6, now_utc=150
        )
    for actor, chat_id, message_id, now in (
        (902, 901, 5, 150),
        (901, 902, 5, 150),
        (901, 901, 6, 150),
        (901, 901, 5, 200),
    ):
        with session_scope() as session:
            assert repo.claim_tool_approval(
                session, "claim-generation", telegram_user_id=actor,
                chat_id=chat_id, message_id=message_id, now_utc=now,
            ) is None
            assert session.get(ToolApproval, approval_id).status == "pending"
            assert session.get(Task, task_id).status == "awaiting_approval"
    with session_scope() as session:
        claimed = repo.claim_tool_approval(
            session, "claim-generation", telegram_user_id=901,
            chat_id=901, message_id=5, now_utc=199,
        )
        assert claimed is not None and claimed.status == "approved"
        assert claimed.decided_at == 199
        assert session.get(Task, task_id).status == "running"
    with session_scope() as session:
        assert repo.claim_tool_approval(
            session, "claim-generation", telegram_user_id=901,
            chat_id=901, message_id=5, now_utc=199,
        ) is None
        assert repo.resolve_tool_approval(
            session, "claim-generation", status="cancelled", now_utc=199,
            telegram_user_id=902, allowed_statuses=("approved",),
        ) is None
        cancelled = repo.resolve_tool_approval(
            session, "claim-generation", status="cancelled", now_utc=199,
            telegram_user_id=901, allowed_statuses=("approved",),
        )
        assert cancelled is not None and cancelled.status == "cancelled"
        assert session.get(Task, task_id).status == "cancelled"


def test_approval_partial_uniques_release_only_after_terminal_transition(db: None) -> None:
    owner, first_task = _approval_task(903)
    _, second_task = _approval_task(903)
    other_owner, _ = _approval_task(904)
    with session_scope() as session:
        first = _pending_approval(session, owner, first_task, "first-generation", 903)
        first_id = first.id
    for user_id, task_id, token, actor in (
        (owner, second_task, "duplicate-user", 903),
        (other_owner, first_task, "duplicate-task", 904),
        (other_owner, second_task, "first-generation", 904),
    ):
        with pytest.raises(IntegrityError), session_scope() as session:
            _pending_approval(session, user_id, task_id, token, actor)
    with session_scope() as session:
        repo.resolve_tool_approval(
            session, "first-generation", status="rejected", now_utc=150,
            telegram_user_id=903,
        )
        second = _pending_approval(session, owner, second_task, "next-generation", 903)
        assert second.id != first_id
        assert session.get(Task, first_task).status == "rejected"
        assert session.get(Task, second_task).status == "awaiting_approval"


def test_approval_task_pair_conflicts_roll_back_both_rows(db: None) -> None:
    owner, task_id = _approval_task(905)
    with session_scope() as session:
        session.get(Task, task_id).status = "completed"
    with pytest.raises(repo.ApprovalTransitionConflict), session_scope() as session:
        _pending_approval(session, owner, task_id, "bad-source", 905)
    with session_scope() as session:
        assert repo.find_tool_approval(session, "bad-source") is None
        session.get(Task, task_id).status = "running"
        _pending_approval(session, owner, task_id, "resolve-conflict", 905)
    with session_scope() as session:
        session.get(Task, task_id).status = "completed"
    with pytest.raises(repo.ApprovalTransitionConflict), session_scope() as session:
        repo.resolve_tool_approval(
            session, "resolve-conflict", status="expired", now_utc=200,
        )
    with session_scope() as session:
        row = repo.find_tool_approval(session, "resolve-conflict")
        assert row.status == "pending" and row.decided_at is None
        assert session.get(Task, task_id).status == "completed"


def test_approval_audit_is_unique_while_legacy_nulls_remain_valid(db: None) -> None:
    owner, task_id = _approval_task(906)
    with session_scope() as session:
        approval = _pending_approval(session, owner, task_id, "audit-generation", 906)
        approval_id = approval.id
        session.add_all([
            ToolCall(task_id=task_id, tool_name="read_file"),
            ToolCall(task_id=task_id, tool_name="read_file"),
            ToolCall(task_id=task_id, approval_id=approval_id, tool_name="create_reminder"),
        ])
    with pytest.raises(IntegrityError), session_scope() as session:
        session.add(ToolCall(
            task_id=task_id, approval_id=approval_id, tool_name="create_reminder"
        ))
    with session_scope() as session:
        assert session.scalars(select(ToolCall.approval_id).order_by(ToolCall.id)).all() == [
            None, None, approval_id,
        ]


def _attempt(session, *, group: str, index: int = 1, **overrides) -> LLMCall:
    values = {
        "purpose": "agent",
        "provider": "openai",
        "model": "priced-model",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "call_group_id": group,
        "attempt_index": index,
        "accounting_status": "reserved",
        "budget_charge_usd": 0.04,
        "accounting_detail": json.dumps({
            "requested_model": "priced-model",
            "returned_model": None,
            "estimator_version": "utf8-json-v1",
            "pricing_version": "test-prices-v1",
            "input_price_per_million": "1.5",
            "output_price_per_million": "2",
            "outcome": "reserved",
            "usage_known": False,
        }),
    }
    values.update(overrides)
    return record_llm_call(session, **values)


def _finish_attempt(session, attempt_id: int, *, status: str = "settled") -> bool:
    return repo.finalize_llm_attempt(
        session,
        attempt_id,
        accounting_status=status,
        model="priced-model-returned",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=0.00025,
        budget_charge_usd=0.00025 if status == "settled" else 0.0,
        latency_ms=12,
        accounting_detail=json.dumps({
            "returned_model": "priced-model-returned",
            "outcome": "success" if status == "settled" else "timeout",
            "usage_known": status == "settled",
        }),
    )


def test_attempt_finalization_is_compare_and_swap_without_duplicate_rows(db: None) -> None:
    with session_scope() as session:
        owner = get_or_create_user(session, 1201, None)
        row_id = _attempt(session, group="one-logical-call", user_id=owner.id).id
    with session_scope() as session:
        assert _finish_attempt(session, row_id)
    with session_scope() as session:
        assert not _finish_attempt(session, row_id, status="unknown")
        assert not _finish_attempt(session, row_id + 100)
        rows = session.scalars(select(LLMCall)).all()
        assert [(row.id, row.accounting_status, row.cost_usd) for row in rows] == [
            (row_id, "settled", 0.00025)
        ]
        usage = repo.total_usage(session)
        assert usage["physical_attempts"] == usage["llm_calls"] == 1
        assert usage["known_cost_usd"] == usage["budget_charge_usd"] == 0.00025
        assert usage["total_tokens"] == 150
        assert usage["unknown_attempts"] == 0


def test_attempt_reservation_and_finalization_obey_transaction_rollback(db: None) -> None:
    with session_scope() as session:
        owner = get_or_create_user(session, 1202, None)
        owner_id = owner.id
    with pytest.raises(RuntimeError, match="reserve rollback"), session_scope() as session:
        _attempt(session, group="rolled-back", user_id=owner_id)
        raise RuntimeError("reserve rollback")
    with session_scope() as session:
        assert session.scalars(select(LLMCall)).all() == []
        row_id = _attempt(session, group="committed-reserve", user_id=owner_id).id
    with pytest.raises(RuntimeError, match="finalize rollback"), session_scope() as session:
        assert _finish_attempt(session, row_id)
        raise RuntimeError("finalize rollback")
    with session_scope() as session:
        row = session.get(LLMCall, row_id)
        assert row.accounting_status == "reserved"
        assert row.cost_usd == 0.0 and row.budget_charge_usd == 0.04
        assert _finish_attempt(session, row_id)


def test_duplicate_attempt_identity_cannot_double_charge_ledger(db: None) -> None:
    with session_scope() as session:
        owner = get_or_create_user(session, 1203, None)
        owner_id = owner.id
        _attempt(session, group="retry-group", user_id=owner_id)
    with pytest.raises(IntegrityError), session_scope() as session:
        _attempt(session, group="retry-group", user_id=owner_id)
    with session_scope() as session:
        _attempt(session, group="retry-group", index=2, user_id=owner_id)
        usage = user_usage(session, session.get(User, owner_id))
        assert usage["llm_calls"] == 1 and usage["physical_attempts"] == 2
        assert usage["budget_charge_usd"] == 0.08 and usage["unknown_attempts"] == 2


def test_unknown_attempt_retains_reserve_instead_of_reporting_free_usage(db: None) -> None:
    with session_scope() as session:
        owner = get_or_create_user(session, 1204, None)
        row_id = _attempt(session, group="ambiguous-timeout", user_id=owner.id).id
    with session_scope() as session:
        assert _finish_attempt(session, row_id, status="unknown")
    with session_scope() as session:
        row = session.get(LLMCall, row_id)
        assert row.accounting_status == "unknown" and row.budget_charge_usd == 0.04
        assert (row.prompt_tokens, row.completion_tokens, row.cost_usd) == (0, 0, 0.0)
        assert not _finish_attempt(session, row_id)
        usage = repo.total_usage(session)
        assert usage["unknown_attempts"] == usage["physical_attempts"] == 1
        assert usage["known_cost_usd"] == usage["cost_usd"] == 0.0
        assert usage["budget_charge_usd"] == 0.04


def test_usage_distinguishes_logical_retries_legacy_known_and_unknown(db: None) -> None:
    with session_scope() as session:
        owner = get_or_create_user(session, 1205, None)
        conversation = get_or_create_conversation(session, owner)
        message = add_message(session, conversation, "user", "request")
        task = repo.create_task(
            session, user_id=owner.id, conversation_id=conversation.id,
            request_message_id=message.id, goal="accounting",
        )
        record_llm_call(
            session, task_id=task.id, purpose="legacy", provider="legacy", model="old",
            prompt_tokens=40, completion_tokens=10, cost_usd=0.01, latency_ms=3,
        )
        for group in ("known-retry", "unknown-retry"):
            first = _attempt(
                session, group=group, user_id=owner.id, task_id=task.id,
                request_message_id=message.id,
            )
            assert _finish_attempt(session, first.id, status="unknown")
            second = _attempt(
                session, group=group, index=2, user_id=owner.id, task_id=task.id,
                request_message_id=message.id,
            )
            if group == "known-retry":
                assert _finish_attempt(session, second.id)
        expected = {
            "llm_calls": 3,
            "physical_attempts": 5,
            "prompt_tokens": 140,
            "completion_tokens": 60,
            "total_tokens": 200,
            "cost_usd": 0.01025,
            "known_cost_usd": 0.01025,
            "budget_charge_usd": 0.13025,
            "unknown_attempts": 3,
        }
        assert user_usage(session, owner) == expected
        assert repo.total_usage(session) == expected
        assert repo.session_stats(session, owner)["usage"] == expected


def test_startup_recovers_reserved_attempts_once_without_refund(tmp_path) -> None:
    database = tmp_path / "interrupted-attempt.db"
    reset_engine_for_tests(f"sqlite:///{database}")
    try:
        init_db()
        with session_scope() as session:
            owner = get_or_create_user(session, 1206, None)
            interrupted_id = _attempt(
                session, group="process-interrupted", user_id=owner.id
            ).id
            settled_id = _attempt(session, group="completed-before-crash", user_id=owner.id).id
            assert _finish_attempt(session, settled_id)
        reset_engine_for_tests(f"sqlite:///{database}")
        init_db()
        init_db()
        with session_scope() as session:
            interrupted = session.get(LLMCall, interrupted_id)
            assert interrupted.accounting_status == "unknown"
            assert interrupted.budget_charge_usd == 0.04
            detail = json.loads(interrupted.accounting_detail)
            assert detail["outcome"] == "interrupted" and detail["usage_known"] is False
            assert detail["input_price_per_million"] == "1.5"
            assert session.get(LLMCall, settled_id).accounting_status == "settled"
            assert repo.interrupt_reserved_llm_calls(session) == 0
            assert not _finish_attempt(session, interrupted_id)
            usage = repo.total_usage(session)
            assert usage["physical_attempts"] == 2
            assert usage["unknown_attempts"] == 1
            assert usage["budget_charge_usd"] == 0.04025
    finally:
        reset_engine_for_tests()


def test_request_attachment_is_exact_owned_scope_and_never_overwrites(db: None) -> None:
    with session_scope() as session:
        alice = get_or_create_user(session, 1207, None)
        bob = get_or_create_user(session, 1208, None)
        conv_a = get_or_create_conversation(session, alice)
        conv_b = get_or_create_conversation(session, bob)
        first = add_message(session, conv_a, "user", "first")
        later = add_message(session, conv_a, "user", "later")
        foreign = add_message(session, conv_b, "user", "foreign")
        task = repo.create_task(
            session, user_id=alice.id, conversation_id=conv_a.id,
            request_message_id=first.id, goal="first task",
        )
        later_task = repo.create_task(
            session, user_id=alice.id, conversation_id=conv_a.id,
            request_message_id=later.id, goal="later task",
        )
        first_row = _attempt(
            session, group="first-request", user_id=alice.id, request_message_id=first.id
        )
        later_row = _attempt(
            session, group="later-request", user_id=alice.id, request_message_id=later.id
        )
        foreign_row = _attempt(
            session, group="foreign-request", user_id=bob.id, request_message_id=foreign.id
        )
        assert repo.attach_request_llm_calls(
            session, user_id=alice.id, request_message_id=first.id, task_id=later_task.id
        ) == 0
        assert repo.attach_request_llm_calls(
            session, user_id=bob.id, request_message_id=foreign.id, task_id=task.id
        ) == 0
        assert repo.attach_request_llm_calls(
            session, user_id=alice.id, request_message_id=first.id, task_id=task.id
        ) == 1
        assert repo.attach_request_llm_calls(
            session, user_id=alice.id, request_message_id=first.id, task_id=task.id
        ) == 0
        assert first_row.task_id == task.id
        assert later_row.task_id is None and foreign_row.task_id is None


def test_forget_detaches_request_and_task_but_preserves_owner_accounting(db: None) -> None:
    with session_scope() as session:
        alice = get_or_create_user(session, 1209, None)
        bob = get_or_create_user(session, 1210, None)
        alice_id = alice.id
        conv_a = get_or_create_conversation(session, alice)
        conv_b = get_or_create_conversation(session, bob)
        request = add_message(session, conv_a, "user", "private request")
        foreign = add_message(session, conv_b, "user", "other private request")
        task = repo.create_task(
            session, user_id=alice.id, conversation_id=conv_a.id,
            request_message_id=request.id, goal="private goal",
        )
        legacy = record_llm_call(
            session, task_id=task.id, purpose="legacy", provider="legacy", model="old",
            prompt_tokens=40, completion_tokens=10, cost_usd=0.01, latency_ms=3,
        )
        settled = _attempt(
            session, group="owned-settled", user_id=alice.id,
            request_message_id=request.id, task_id=task.id,
        )
        assert _finish_attempt(session, settled.id)
        unknown = _attempt(
            session, group="pre-task-unknown", user_id=alice.id, request_message_id=request.id
        )
        assert _finish_attempt(session, unknown.id, status="unknown")
        foreign_row = _attempt(
            session, group="foreign", user_id=bob.id, request_message_id=foreign.id
        )
        owned_ids = [legacy.id, settled.id, unknown.id]
        before = user_usage(session, alice)
        foreign_id, foreign_request_id = foreign_row.id, foreign.id
        before_detail = unknown.accounting_detail
    with session_scope() as session:
        repo.wipe_session(session, session.get(User, alice_id))
    with session_scope() as session:
        alice = session.get(User, alice_id)
        assert user_usage(session, alice) == before
        for row_id in owned_ids:
            row = session.get(LLMCall, row_id)
            assert row.user_id == alice_id
            assert row.task_id is None and row.request_message_id is None
        assert session.get(LLMCall, unknown.id).accounting_detail == before_detail
        assert session.get(LLMCall, foreign_id).request_message_id == foreign_request_id
        assert session.get(Message, request.id) is None and session.get(Task, task.id) is None
        assert session.get(Message, foreign_request_id) is not None
        assert (
            session.scalar(select(Conversation.id).where(Conversation.user_id == alice_id))
            is None
        )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


def test_forget_rejects_conflicting_request_owner_and_rolls_back(db: None) -> None:
    with session_scope() as session:
        alice = get_or_create_user(session, 1211, None)
        bob = get_or_create_user(session, 1212, None)
        conv = get_or_create_conversation(session, alice)
        message = add_message(session, conv, "user", "must survive")
        call = _attempt(
            session, group="owner-conflict", user_id=bob.id, request_message_id=message.id
        )
        reminder = repo.create_reminder(
            session, user_id=alice.id, message="also survive", timezone="UTC", due_at_utc=500
        )
        owner_id, call_id, message_id, reminder_id = alice.id, call.id, message.id, reminder.id
    with pytest.raises(ValueError, match="ownership"), session_scope() as session:
        repo.wipe_session(session, session.get(User, owner_id))
    with session_scope() as session:
        assert session.get(Message, message_id).content == "must survive"
        assert session.get(LLMCall, call_id).request_message_id == message_id
        assert repo.reminder_for_owner(session, owner_id, reminder_id).message == "also survive"


@pytest.mark.parametrize("detail", [
    '{"prompt":"private"}',
    '{"outcome":"raw provider error"}',
    '{"usage_known":0}',
    '{"input_price_per_million":"NaN"}',
    '["not", "metadata"]',
])
def test_attempt_metadata_rejects_nonaccounting_data_without_inserting(db: None, detail) -> None:
    with pytest.raises(ValueError), session_scope() as session:
        _attempt(session, group="invalid-detail", accounting_detail=detail)
    with session_scope() as session:
        assert session.scalars(select(LLMCall)).all() == []


def test_budget_session_clamps_busy_timeout_and_restores_pool_after_commit_and_rollback(
    db: None,
) -> None:
    with get_engine().connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=321")
    with session_scope(timeout_seconds=0.0259) as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 25
        owner_id = get_or_create_user(session, 1213, None).id
    with session_scope() as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 321
        assert session.get(User, owner_id).telegram_user_id == 1213
    with pytest.raises(RuntimeError, match="rollback"), session_scope(timeout_seconds=0) as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 0
        session.get(User, owner_id).username = "not committed"
        raise RuntimeError("rollback")
    with session_scope(timeout_seconds=5) as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 321
        assert session.get(User, owner_id).username is None
    with session_scope() as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 321


def test_zero_budget_timeout_fails_locked_write_without_losing_reservation(tmp_path) -> None:
    database = tmp_path / "locked-budget.db"
    reset_engine_for_tests(f"sqlite:///{database}")
    blocker = None
    try:
        init_db()
        with session_scope() as session:
            owner = get_or_create_user(session, 1214, None)
            row_id = _attempt(session, group="locked-finalization", user_id=owner.id).id
        with get_engine().connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=211")
        blocker = sqlite3.connect(database)
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(OperationalError, match="database is locked"), session_scope(
            timeout_seconds=0
        ) as session:
            _finish_attempt(session, row_id)
        blocker.rollback()
        with session_scope() as session:
            assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 211
            row = session.get(LLMCall, row_id)
            assert row.accounting_status == "reserved" and row.budget_charge_usd == 0.04
            assert _finish_attempt(session, row_id)
    finally:
        if blocker is not None:
            blocker.close()
        reset_engine_for_tests()


@pytest.mark.parametrize("timeout", [-0.01, float("nan"), float("inf")])
def test_budget_session_rejects_nonfinite_or_negative_timeout(db: None, timeout) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"), session_scope(
        timeout_seconds=timeout
    ):
        pytest.fail("invalid timeout entered a transaction")
