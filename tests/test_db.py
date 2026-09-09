"""Kiểm thử tầng lưu trữ: models, engine in-memory và các helper repo."""

import json

import pytest

from laplace.db import init_db, reset_engine_for_tests, session_scope
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
        }

        empty = user_usage(session, bob)
        assert empty == {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }


def test_record_trace_serializes_payload(db: None) -> None:
    payload = {"step": "plan", "note": "tiếng Việt", "n": 3}
    with session_scope() as session:
        user = get_or_create_user(session, 111, None)
        conversation = get_or_create_conversation(session, user)
        trace = record_trace(
            session, conversation_id=conversation.id, step_index=0, payload=payload
        )

        assert trace.task_id is None
        assert json.loads(trace.payload_json) == payload
        assert "tiếng Việt" in trace.payload_json  # ensure_ascii=False giữ Unicode
