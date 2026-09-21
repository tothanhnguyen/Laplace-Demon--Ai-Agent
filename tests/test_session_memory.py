"""Regression tests for session migration, ownership, guards, and forgetting."""

import json
import queue
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select, text

from laplace import repo
from laplace.context import message_chars, parse_summary
from laplace.db import get_engine, init_db, reset_engine_for_tests, session_scope
from laplace.llm.base import LLMResult
from laplace.models import (
    Conversation,
    LLMCall,
    Message,
    Step,
    Task,
    ToolCall,
    Trace,
    User,
)
from laplace.prompts import system_message
from laplace.services import chat
from laplace.services.chat import ChatReply, _run_reserved_message
from laplace.services.memory import (
    forget_memory,
    load_memory,
    release_turn,
    request_cancel,
    try_reserve_turn,
    user_busy,
)
from laplace.tools.base import load_builtin_tools


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch) -> None:
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    monkeypatch.setattr(
        chat, "Settings", lambda: SimpleNamespace(context_max_chars=12_000)
    )
    yield
    reset_engine_for_tests(None)


def test_sprint2_schema_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                username VARCHAR(64),
                created_at DATETIME NOT NULL
            );
            CREATE TABLE conversations (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at DATETIME NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER NOT NULL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE tasks (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                conversation_id INTEGER REFERENCES conversations(id),
                goal TEXT NOT NULL,
                status VARCHAR(24) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            INSERT INTO users VALUES (1, 101, 'legacy', CURRENT_TIMESTAMP);
            INSERT INTO conversations VALUES (1, 1, CURRENT_TIMESTAMP);
            INSERT INTO messages VALUES (1, 1, 'user', 'preserve me', CURRENT_TIMESTAMP);
            INSERT INTO tasks VALUES (
                1, 1, 1, 'legacy task', 'running', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
        )

    reset_engine_for_tests(f"sqlite:///{database}")
    init_db()
    engine = get_engine()
    first_columns = {
        table: {column["name"] for column in inspect(engine).get_columns(table)}
        for table in ("conversations", "tasks")
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE conversations SET summary=:summary, "
                "summary_until_message_id=:watermark WHERE id=1"
            ),
            {"summary": '{"version":1,"entries":[],"omitted_count":0}', "watermark": 1},
        )

    init_db()

    assert {"summary", "summary_until_message_id"} <= first_columns["conversations"]
    assert "request_message_id" in first_columns["tasks"]
    with session_scope() as session:
        assert session.scalar(select(Message.content).where(Message.id == 1)) == "preserve me"
        task = session.get(Task, 1)
        conversation = session.execute(
            text(
                "SELECT summary, summary_until_message_id FROM conversations WHERE id=1"
            )
        ).one()
        assert task is not None
        assert task.request_message_id is None
        assert tuple(conversation) == (
            '{"version":1,"entries":[],"omitted_count":0}',
            1,
        )
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


def _seed_owned_memory() -> tuple[int, int, dict, dict]:
    with session_scope() as session:
        alice = repo.get_or_create_user(session, 1001, "alice")
        bob = repo.get_or_create_user(session, 2002, "bob")

        alice_conversation = repo.get_or_create_conversation(session, alice)
        alice_request = repo.add_message(session, alice_conversation, "user", "alice secret")
        repo.add_message(session, alice_conversation, "assistant", "alice result")
        alice_task = repo.create_task(
            session,
            user_id=alice.id,
            conversation_id=alice_conversation.id,
            request_message_id=alice_request.id,
            goal="alice task",
        )
        repo.append_step(
            session,
            task_id=alice_task.id,
            step_index=0,
            name="fixture",
            status="completed",
            detail={"action": {}, "observation": {}, "task_state": {}, "stop_reason": None},
        )
        repo.record_tool_call(
            session,
            task_id=alice_task.id,
            tool_name="fixture",
            params_json="{}",
            result_json='{"ok":true,"payload":"alice tool result"}',
            ok=True,
            error=None,
            latency_ms=0,
        )
        repo.record_trace(
            session,
            task_id=alice_task.id,
            conversation_id=alice_conversation.id,
            step_index=0,
            payload={"owner": "alice"},
        )

        bob_conversation = repo.get_or_create_conversation(session, bob)
        bob_request = repo.add_message(session, bob_conversation, "user", "bob secret")
        repo.add_message(session, bob_conversation, "assistant", "bob result")
        bob_task = repo.create_task(
            session,
            user_id=bob.id,
            conversation_id=bob_conversation.id,
            request_message_id=bob_request.id,
            goal="bob task",
        )
        repo.append_step(
            session,
            task_id=bob_task.id,
            step_index=0,
            name="fixture",
            status="completed",
            detail={"action": {}, "observation": {}, "task_state": {}, "stop_reason": None},
        )
        repo.record_tool_call(
            session,
            task_id=bob_task.id,
            tool_name="fixture",
            params_json="{}",
            result_json='{"ok":true,"payload":"bob tool result"}',
            ok=True,
            error=None,
            latency_ms=0,
        )
        repo.record_trace(
            session,
            task_id=bob_task.id,
            conversation_id=bob_conversation.id,
            step_index=0,
            payload={"owner": "bob"},
        )

        for user_id, task_id, prompt, completion in (
            (alice.id, None, 10, 1),
            (None, alice_task.id, 20, 2),
            (alice.id, alice_task.id, 30, 3),
            (bob.id, bob_task.id, 40, 4),
        ):
            repo.record_llm_call(
                session,
                user_id=user_id,
                task_id=task_id,
                purpose="fixture",
                provider="fixture",
                model="fixture",
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=0.0,
                latency_ms=0,
            )

        alice_usage = repo.user_usage(session, alice)
        bob_usage = repo.user_usage(session, bob)
        return alice.id, bob.id, alice_usage, bob_usage


def test_forget_is_user_isolated_fk_safe_idempotent_and_preserves_usage() -> None:
    alice_id, bob_id, alice_usage, bob_usage = _seed_owned_memory()

    status, deleted = forget_memory(1001)

    assert status == "deleted"
    assert deleted is not None
    assert deleted["conversations"] == 1
    assert deleted["tasks"] == 1
    assert deleted["usage_llm_calls"] == alice_usage["llm_calls"]

    alice_memory = load_memory(1001)
    bob_memory = load_memory(2002)
    assert alice_memory is not None
    assert alice_memory["conversations"] == 0
    assert alice_memory["messages"] == 0
    assert alice_memory["tasks"] == 0
    assert alice_memory["tool_calls"] == 0
    assert alice_memory["usage"] == alice_usage
    assert bob_memory is not None
    assert bob_memory["conversations"] == 1
    assert bob_memory["messages"] == 2
    assert bob_memory["tasks"] == 1
    assert bob_memory["tool_calls"] == 1
    assert bob_memory["usage"] == bob_usage
    assert [item["content"] for item in bob_memory["recent_messages"]] == [
        "bob secret",
        "bob result",
    ]

    with session_scope() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert (
            session.scalars(select(Task).where(Task.user_id == alice_id)).all()
            == []
        )
        remaining_tasks = list(session.scalars(select(Task)))
        remaining_steps = list(session.scalars(select(Step)))
        remaining_tools = list(session.scalars(select(ToolCall)))
        remaining_traces = list(session.scalars(select(Trace)))
        assert len(remaining_tasks) == 1
        assert remaining_tasks[0].user_id == bob_id
        assert [step.task_id for step in remaining_steps] == [remaining_tasks[0].id]
        assert [tool.task_id for tool in remaining_tools] == [remaining_tasks[0].id]
        assert [trace.task_id for trace in remaining_traces] == [remaining_tasks[0].id]
        alice_calls = session.scalars(
            select(LLMCall).where(LLMCall.user_id == alice_id)
        ).all()
        assert len(alice_calls) == 3
        assert all(call.task_id is None for call in alice_calls)
        assert session.scalar(select(User).where(User.id == bob_id)) is not None

    repeated_status, repeated = forget_memory(1001)
    assert repeated_status == "deleted"
    assert repeated is not None
    assert repeated["conversations"] == 0
    assert repeated["tasks"] == 0
    repeated_memory = load_memory(1001)
    assert repeated_memory is not None
    assert repeated_memory["usage"] == alice_usage


def test_forget_rejects_cross_owner_trace_and_rolls_back() -> None:
    alice_id, _, _, _ = _seed_owned_memory()
    with session_scope() as session:
        alice_task = session.scalar(select(Task).where(Task.user_id == alice_id))
        bob_conversation = session.scalar(
            select(Conversation)
            .join(User, Conversation.user_id == User.id)
            .where(User.telegram_user_id == 2002)
        )
        assert alice_task is not None
        assert bob_conversation is not None
        trace = session.scalar(select(Trace).where(Trace.task_id == alice_task.id))
        assert trace is not None
        trace.conversation_id = bob_conversation.id

    with pytest.raises(ValueError, match="ownership mâu thuẫn"):
        forget_memory(1001)

    with session_scope() as session:
        assert session.scalar(
            select(Task).where(Task.user_id == alice_id)
        ) is not None
        assert session.scalar(
            select(Conversation)
            .join(User, Conversation.user_id == User.id)
            .where(User.telegram_user_id == 1001)
        ) is not None


def test_memory_snapshot_tolerates_legacy_json_top_level_types() -> None:
    with session_scope() as session:
        user = repo.get_or_create_user(session, 3001, "legacy-json")
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(session, conversation, "user", "inspect")
        task = repo.create_task(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            request_message_id=request.id,
            goal="legacy payload",
        )
        session.add(
            Step(
                task_id=task.id,
                step_index=0,
                name="legacy",
                status="running",
                detail='{"task_state":{"remaining_work":"bad"}}',
            )
        )
        session.add(
            ToolCall(
                task_id=task.id,
                tool_name="read_file",
                params_json="[]",
                result_json="[]",
                ok=True,
            )
        )
        session.flush()

        snapshot = repo.session_stats(session, user)
        observations = repo.tool_rows_for_request_ids(session, [request.id])

    assert snapshot["recent_tasks"][0]["remaining"] is None
    assert snapshot["recent_tasks"][0]["tools"][0]["source"] == ""
    assert observations[0]["payload"] == ""
    assert observations[0]["source"] is None


def test_compaction_queries_bound_large_message_and_tool_payloads() -> None:
    with session_scope() as session:
        user = repo.get_or_create_user(session, 3002, "bounded-query")
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(
            session,
            conversation,
            "user",
            "MESSAGE_HEAD" + "m" * 5_000 + "MESSAGE_TAIL",
        )
        task = repo.create_task(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            request_message_id=request.id,
            goal="bounded tool",
        )
        session.add(
            ToolCall(
                task_id=task.id,
                tool_name="read_file",
                params_json='{"path":"large.txt"}',
                result_json=json.dumps(
                    {
                        "ok": True,
                        "payload": "TOOL_HEAD" + "t" * 5_000 + "TOOL_TAIL",
                    }
                ),
                ok=True,
            )
        )
        session.flush()

        page = repo.conversation_message_page(
            session,
            conversation.id,
            after_id=None,
            before_id=request.id + 1,
            limit=8,
        )
        tools = repo.tool_rows_for_request_ids(session, [request.id])

    assert len(page[0]["content"]) <= repo.DB_EXCERPT_CHARS
    assert "MESSAGE_HEAD" in page[0]["content"]
    assert "MESSAGE_TAIL" in page[0]["content"]
    assert len(tools[0]["payload"]) <= repo.DB_EXCERPT_CHARS
    assert "TOOL_HEAD" in tools[0]["payload"]
    assert "TOOL_TAIL" in tools[0]["payload"]


def test_turn_reservation_is_per_user_and_cancel_only_targets_chat() -> None:
    chat_lease = try_reserve_turn(3101)
    other_user = try_reserve_turn(3102)
    assert chat_lease is not None
    assert other_user is not None
    try:
        assert try_reserve_turn(3101) is None
        assert user_busy(3101)
        assert request_cancel(3101)
        assert chat_lease.cancel_event.is_set()
        assert not other_user.cancel_event.is_set()
    finally:
        release_turn(chat_lease)
        release_turn(other_user)

    forget_lease = try_reserve_turn(3101, kind="forget")
    assert forget_lease is not None
    try:
        assert not request_cancel(3101)
        assert not forget_lease.cancel_event.is_set()
    finally:
        release_turn(forget_lease)
    assert not user_busy(3101)
    assert request_cancel(3101) is False


class _BlockingToolProvider:
    name = "blocking-fixture"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.allow_return = threading.Event()
        self.calls = 0

    def complete(self, messages, *, json_schema=None) -> LLMResult:
        self.calls += 1
        self.started.set()
        if not self.allow_return.wait(timeout=5):
            raise RuntimeError("test provider was not released")
        return LLMResult(
            parsed={
                "action": "tool",
                "tool": "read_file",
                "params": {"path": "unused.txt"},
            },
            model="blocking-fixture",
            prompt_tokens=7,
            completion_tokens=3,
        )


def test_cooperative_cancel_waits_for_provider_and_prevents_the_tool(
    tmp_path: Path,
) -> None:
    reset_engine_for_tests(f"sqlite:///{tmp_path / 'threaded.db'}")
    init_db()
    provider = _BlockingToolProvider()
    lease = try_reserve_turn(4101)
    assert lease is not None
    outcomes: queue.Queue[ChatReply | BaseException] = queue.Queue()

    def run_worker() -> None:
        try:
            outcomes.put(
                _run_reserved_message(
                    lease,
                    4101,
                    "thread-user",
                    "read a file",
                    provider=provider,
                )
            )
        except BaseException as exc:
            outcomes.put(exc)



    worker = threading.Thread(target=run_worker)
    worker.start()
    try:
        assert provider.started.wait(timeout=5)
        assert user_busy(4101)
        assert forget_memory(4101) == ("busy", None)
        assert request_cancel(4101)
        assert lease.cancel_event.is_set()
        assert user_busy(4101)
    finally:
        provider.allow_return.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    outcome = outcomes.get_nowait()
    if isinstance(outcome, BaseException):
        raise outcome
    assert outcome.usage["llm_calls"] == 1
    assert outcome.usage["total_tokens"] == 10
    assert provider.calls == 1
    assert not user_busy(4101)

    memory = load_memory(4101)
    assert memory is not None
    assert memory["messages"] == 2
    assert memory["tasks"] == 0
    assert memory["tool_calls"] == 0
    with session_scope() as session:
        traces = [
            json.loads(row.payload_json)
            for row in session.scalars(select(Trace)).all()
        ]
    assert [trace["event"] for trace in traces] == ["cancelled"]
class _SummaryProvider:
    name = "summary-fixture"

    def complete(self, messages, *, json_schema=None) -> LLMResult:
        return LLMResult(
            parsed={"version": 1, "entries": [], "omitted_count": 0},
            model="summary-fixture",
            prompt_tokens=2,
            completion_tokens=1,
        )


def test_compaction_usage_write_failure_does_not_advance_watermark(
    monkeypatch,
) -> None:
    user_id = 5100
    with session_scope() as session:
        user = repo.get_or_create_user(session, user_id, "usage-failure")
        conversation = repo.get_or_create_conversation(session, user)
        for index in range(6):
            repo.add_message(
                session,
                conversation,
                "user",
                f"old-{index} " + "x" * 2_000,
            )
            repo.add_message(session, conversation, "assistant", "stored")
        request = repo.add_message(session, conversation, "user", "current")
        conversation_id = conversation.id
        request_id = request.id
        internal_user_id = user.id

    def fail_usage_write(*args, **kwargs):
        raise RuntimeError("usage write failed")

    monkeypatch.setattr(chat, "_record_llm_result", fail_usage_write)
    with pytest.raises(RuntimeError, match="usage write failed"):
        chat._maybe_compact(
            provider=_SummaryProvider(),
            recorder=chat._TurnRecorder(),
            user_id=internal_user_id,
            conversation_id=conversation_id,
            request_message_id=request_id,
            request_text="current",
            max_chars=len(system_message()["content"]) + 2_500,
        )

    assert _latest_summary(user_id) == (None, None)


class _ContextAwareMock:
    name = "mock"

    def __init__(self, fact: str) -> None:
        self.fact = fact
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, json_schema=None) -> LLMResult:
        snapshot = [dict(message) for message in messages]
        self.calls.append(snapshot)
        joined = "\n".join(message["content"] for message in snapshot)
        asks_for_recall = any(
            message["content"] == "recall the early fact" for message in snapshot
        )
        answer = "recalled" if asks_for_recall and self.fact in joined else "ok"
        return LLMResult(
            parsed={"action": "final", "final_answer": answer},
            model="context-aware-mock",
            prompt_tokens=1,
            completion_tokens=1,
        )


def _run_context_turn(
    user_id: int, text_value: str, provider: _ContextAwareMock
) -> ChatReply:
    lease = try_reserve_turn(user_id)
    assert lease is not None
    return _run_reserved_message(
        lease,
        user_id,
        "summary-user",
        text_value,
        provider=provider,
    )


def _latest_summary(user_id: int) -> tuple[str | None, int | None]:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == user_id))
        assert user is not None
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.id.desc())
        )
        assert conversation is not None
        return conversation.summary, conversation.summary_until_message_id


def test_compaction_recalls_early_fact_once_and_respects_every_call_budget(
    monkeypatch,
) -> None:
    load_builtin_tools()
    fact = "EARLY_FACT=blue-orchid"
    provider = _ContextAwareMock(fact)
    user_id = 5101
    long_message_chars = 1_000
    seed_count = 8
    system_chars = len(system_message()["content"])
    budget = (
        system_chars
        + seed_count * long_message_chars
        + (seed_count - 1) * len("ok")
    )
    monkeypatch.setattr(
        chat, "Settings", lambda: SimpleNamespace(context_max_chars=budget)
    )

    watermarks: list[int | None] = []
    for index in range(seed_count):
        prefix = f"turn-{index}: "
        if index == 0:
            prefix += fact + " "
        request = prefix + (" " * (long_message_chars - len(prefix)))
        assert len(request) == long_message_chars
        reply = _run_context_turn(user_id, request, provider)
        assert reply.text == "ok"
        watermarks.append(_latest_summary(user_id)[1])

    assert watermarks == [None] * seed_count
    assert message_chars(provider.calls[-1]) == budget

    recalled = _run_context_turn(user_id, "recall the early fact", provider)
    persisted_summary, compacted_watermark = _latest_summary(user_id)
    watermarks.append(compacted_watermark)

    assert recalled.text == "recalled"
    assert persisted_summary is not None
    assert compacted_watermark is not None
    summary_document = parse_summary(persisted_summary)
    source_ids = [
        source_id
        for entry in summary_document.entries
        for source_id in entry.source_message_ids
    ]
    assert any(fact in entry.text for entry in summary_document.entries)
    assert len(source_ids) == len(set(source_ids))

    trigger_input = provider.calls[-1]
    state_messages = [
        message
        for message in trigger_input
        if message["content"].startswith("SESSION_STATE_DATA")
    ]
    assert len(state_messages) == 1
    state = json.loads(state_messages[0]["content"].split("\n", 1)[1])
    assert state["rolling_summary"] == persisted_summary
    assert fact in state["rolling_summary"]
    assert all(
        fact not in message["content"]
        for message in trigger_input
        if message is not state_messages[0]
    )

    _run_context_turn(user_id, "short follow-up", provider)
    stable_summary, stable_watermark = _latest_summary(user_id)
    watermarks.append(stable_watermark)

    assert stable_summary == persisted_summary
    assert stable_watermark == compacted_watermark
    assert (
        parse_summary(stable_summary).model_dump()
        == summary_document.model_dump()
    )
    numeric_watermarks = [watermark or 0 for watermark in watermarks]
    assert numeric_watermarks == sorted(numeric_watermarks)
    assert numeric_watermarks[-2] > numeric_watermarks[-3]
    assert all(message_chars(messages) <= budget for messages in provider.calls)



def test_oversized_required_request_skips_compaction_and_provider(
    monkeypatch,
) -> None:
    provider = _ContextAwareMock("unused")
    user_id = 5102
    budget = len(system_message()["content"]) + 8
    monkeypatch.setattr(
        chat, "Settings", lambda: SimpleNamespace(context_max_chars=budget)
    )
    with session_scope() as session:
        user = repo.get_or_create_user(session, user_id, "overflow-user")
        conversation = repo.get_or_create_conversation(session, user)
        repo.add_message(session, conversation, "user", "old request")
        repo.add_message(session, conversation, "assistant", "old answer")

    reply = _run_context_turn(user_id, "request too large", provider)
    summary, watermark = _latest_summary(user_id)

    assert "vượt ngân sách context" in reply.text
    assert provider.calls == []
    assert summary is None
    assert watermark is None


def test_two_page_compaction_backlog_is_explicit_in_context(monkeypatch) -> None:
    provider = _ContextAwareMock("unused")
    user_id = 5103
    budget = len(system_message()["content"]) + 2_200
    monkeypatch.setattr(
        chat, "Settings", lambda: SimpleNamespace(context_max_chars=budget)
    )
    with session_scope() as session:
        user = repo.get_or_create_user(session, user_id, "backlog-user")
        conversation = repo.get_or_create_conversation(session, user)
        for index in range(30):
            repo.add_message(
                session,
                conversation,
                "user",
                f"history-{index} " + "x" * 400,
            )
            repo.add_message(session, conversation, "assistant", "stored")

    reply = _run_context_turn(user_id, "continue", provider)
    state_messages = [
        message
        for message in provider.calls[-1]
        if message["content"].startswith("SESSION_STATE_DATA")
    ]

    assert reply.text == "ok"
    assert len(state_messages) == 1
    assert "UNSUMMARIZED_HISTORY_BACKLOG" in state_messages[0]["content"]

def test_forget_keeps_legacy_unowned_tool_rows_and_counts_them() -> None:
    with session_scope() as session:
        repo.get_or_create_user(session, 6101, "legacy")
        session.add(ToolCall(tool_name="read_file"))  # Sprint 2 row: task_id NULL

    status, result = forget_memory(6101)

    assert status == "deleted"
    assert result is not None
    assert result["legacy_unowned_tool_calls"] == 1
    with session_scope() as session:
        assert session.scalar(select(ToolCall.tool_name)) == "read_file"
