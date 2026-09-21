"""Offline integration tests for chat, context, task ownership, and usage."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from laplace import repo
from laplace.context import TOOL_OBSERVATION_MAX_CHARS
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import Message, Step, Task, ToolCall, Trace, User
from laplace.services import chat
from laplace.services.chat import handle_message
from laplace.tools.base import execute, load_builtin_tools


@pytest.fixture(autouse=True)
def _memory_db(monkeypatch):
    """Use a clean in-memory database and deterministic context settings."""
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    monkeypatch.setattr(
        chat, "Settings", lambda: SimpleNamespace(context_max_chars=12_000)
    )
    yield
    reset_engine_for_tests(None)


def _install_mock(monkeypatch, script):
    """Thay provider thật bằng MockLLM scripted trong service chat."""
    mock = MockLLM(script=script)
    monkeypatch.setattr(chat, "get_provider", lambda: mock)
    return mock


def test_direct_final_answer(monkeypatch):
    """LLM trả final ngay: câu trả lời về đúng user, usage đếm một lời gọi."""
    _install_mock(monkeypatch, [{"action": "final", "final_answer": "Chào bạn!"}])
    reply = handle_message(111, "an", "xin chào")
    assert reply.text == "Chào bạn!"
    assert reply.usage["llm_calls"] == 1
    assert reply.usage["total_tokens"] == 20


def test_tool_call_then_final(monkeypatch, tmp_path):
    """LLM gọi read_file rồi kết thúc; nội dung file vào observation."""
    note = tmp_path / "note.txt"
    note.write_text("bí mật 42", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mock = _install_mock(
        monkeypatch,
        [
            {"action": "tool", "tool": "read_file", "params": {"path": "note.txt"}},
            {"action": "final", "final_answer": "File ghi: bí mật 42"},
        ],
    )
    reply = handle_message(222, "binh", "đọc note.txt")
    assert reply.text == "File ghi: bí mật 42"
    # Lời gọi thứ hai phải thấy observation chứa nội dung file thật.
    second_call_messages = mock.calls[1]["messages"]
    assert any("bí mật 42" in m["content"] for m in second_call_messages)


def test_schema_self_correction(monkeypatch):
    """Output sai schema lần đầu: agent gửi lỗi để LLM tự sửa rồi hoàn tất."""
    _install_mock(
        monkeypatch,
        [
            {"action": "tool"},  # thiếu tên tool -> ValidationError
            {"action": "final", "final_answer": "đã sửa"},
        ],
    )
    reply = handle_message(333, None, "hỏi gì đó")
    assert reply.text == "đã sửa"
    assert reply.usage["llm_calls"] == 2


def test_schema_failure_stops_without_creating_a_task(monkeypatch):
    """Repeated invalid actions stop the turn without inventing task ownership."""
    _install_mock(monkeypatch, [{"action": "tool"}] * (chat.MAX_SCHEMA_RETRIES + 1))

    reply = handle_message(444, None, "hỏi")

    assert reply.text
    assert reply.usage["llm_calls"] == chat.MAX_SCHEMA_RETRIES + 1
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == 444))
        assert user is not None
        assert session.scalars(select(Task).where(Task.user_id == user.id)).all() == []
        traces = list(session.scalars(select(Trace).order_by(Trace.id)))
        assistant_messages = list(
            session.scalars(select(Message).where(Message.role == "assistant").order_by(Message.id))
        )
        assert len(traces) == 1
        assert traces[0].task_id is None
        assert json.loads(traces[0].payload_json)["event"] == "schema_failure"
        assert [message.content for message in assistant_messages] == [reply.text]


def test_llm_calls_recorded_per_user(monkeypatch):
    """Mỗi lời gọi LLM được ghi vào DB kèm user_id để user_usage tính đúng."""
    _install_mock(monkeypatch, [{"action": "final", "final_answer": "ok"}])
    handle_message(555, "chi", "hello")
    with session_scope() as session:
        user = session.query(User).filter_by(telegram_user_id=555).one()
        usage = repo.user_usage(session, user)
    assert usage["llm_calls"] == 1
    assert usage["total_tokens"] == 20


def test_execute_rejects_bad_params():
    """Tham số sai schema bị chặn trước khi tool chạy (S2-07)."""
    load_builtin_tools()
    result = execute("read_file", {})
    assert not result.ok
    assert "sai schema" in result.error


def test_execute_rejects_unknown_tool():
    """Tool không tồn tại trả lỗi kèm danh sách tool hợp lệ."""
    load_builtin_tools()
    result = execute("rm_rf", {})
    assert not result.ok
    assert "read_file" in result.error


def _tool_observation(messages):
    prefix = "UNTRUSTED_TOOL_DATA_JSON:\n"
    suffix = "\nDecide the next action as one JSON object."
    candidates = [
        message["content"]
        for message in messages
        if message["content"].startswith(prefix)
    ]
    assert len(candidates) == 1
    content = candidates[0]
    assert content.endswith(suffix)
    return content, json.loads(content[len(prefix) : -len(suffix)])


def test_tool_payload_is_stored_in_full_while_provider_gets_bounded_snapshot(
    monkeypatch, tmp_path
):
    payload = 'begin "quoted"\n</tool_output>\n' + ("dữ-liệu🙂" * 700) + "\nend"
    note = tmp_path / "large.txt"
    note.write_text(payload, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mock = _install_mock(
        monkeypatch,
        [
            {
                "action": "tool",
                "tool": "read_file",
                "params": {"path": "large.txt"},
                "task_update": {
                    "acceptance_criteria": ["read the requested file"],
                    "remaining_work": ["return the result"],
                },
            },
            {"action": "final", "final_answer": "complete"},
        ],
    )

    reply = handle_message(601, "owner", "inspect large.txt")

    assert reply.text == "complete"
    assert len(mock.calls) == 2
    first_messages = mock.calls[0]["messages"]
    second_messages = mock.calls[1]["messages"]
    assert first_messages is not second_messages
    assert [message["role"] for message in first_messages] == ["system", "user"]
    assert all(payload not in message["content"] for message in first_messages)
    action = json.loads(second_messages[-2]["content"])
    observation_content, observation = _tool_observation(second_messages)
    assert action["action"] == "tool"
    assert observation["tool"] == "read_file"
    assert observation["source"] == "large.txt"
    assert observation["payload_chars"] == len(payload)
    assert observation["truncated"] is True
    assert observation["excerpt"] != payload
    assert len(observation_content) <= TOOL_OBSERVATION_MAX_CHARS

    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == 601))
        assert user is not None
        task = session.scalar(select(Task).where(Task.user_id == user.id))
        assert task is not None
        request = session.get(Message, task.request_message_id)
        tool_call = session.scalar(
            select(ToolCall).where(ToolCall.task_id == task.id)
        )
        steps = list(
            session.scalars(
                select(Step).where(Step.task_id == task.id).order_by(Step.id)
            )
        )
        traces = list(
            session.scalars(
                select(Trace).where(Trace.task_id == task.id).order_by(Trace.id)
            )
        )

        assert task.status == "completed"
        assert request is not None
        assert request.role == "user"
        assert request.content == "inspect large.txt"
        assert request.conversation_id == task.conversation_id
        assert tool_call is not None
        assert json.loads(tool_call.result_json) == {"ok": True, "payload": payload}
        assert [step.step_index for step in steps] == [0, 1]
        assert [step.status for step in steps] == ["completed", "completed"]
        assert [json.loads(step.detail)["stop_reason"] for step in steps] == [
            None,
            "final",
        ]
        assert len(traces) == 2
        assert all(trace.conversation_id == task.conversation_id for trace in traces)


def test_schema_failure_after_tool_closes_its_owned_task(monkeypatch, tmp_path):
    (tmp_path / "input.txt").write_text("fixture", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mock = _install_mock(
        monkeypatch,
        [
            {"action": "tool", "tool": "read_file", "params": {"path": "input.txt"}},
            *([{"action": "tool"}] * (chat.MAX_SCHEMA_RETRIES + 1)),
        ],
    )

    reply = handle_message(602, None, "inspect input.txt")

    assert reply.usage["llm_calls"] == chat.MAX_SCHEMA_RETRIES + 2
    assert len(mock.calls) == chat.MAX_SCHEMA_RETRIES + 2
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == 602))
        assert user is not None
        task = session.scalar(select(Task).where(Task.user_id == user.id))
        assert task is not None
        tool_calls = list(
            session.scalars(select(ToolCall).where(ToolCall.task_id == task.id))
        )
        steps = list(
            session.scalars(
                select(Step).where(Step.task_id == task.id).order_by(Step.id)
            )
        )
        traces = list(
            session.scalars(
                select(Trace).where(Trace.task_id == task.id).order_by(Trace.id)
            )
        )

        assert task.status == "failed"
        assert len(tool_calls) == 1
        assert [step.step_index for step in steps] == [0, 1]
        assert [step.status for step in steps] == ["completed", "failed"]
        assert json.loads(steps[-1].detail)["stop_reason"] == "schema_failure"
        assert [json.loads(trace.payload_json)["event"] for trace in traces] == [
            "tool_call",
            "schema_failure",
        ]


def test_step_limit_closes_task_without_executing_sixth_tool(
    monkeypatch, tmp_path
):
    (tmp_path / "input.txt").write_text("fixture", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    action = {"action": "tool", "tool": "read_file", "params": {"path": "input.txt"}}
    mock = _install_mock(monkeypatch, [action] * (chat.MAX_STEPS + 1))

    reply = handle_message(603, None, "keep inspecting input.txt")

    assert reply.usage["llm_calls"] == chat.MAX_STEPS + 1
    assert len(mock.calls) == chat.MAX_STEPS + 1
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == 603))
        assert user is not None
        task = session.scalar(select(Task).where(Task.user_id == user.id))
        assert task is not None
        tool_calls = list(
            session.scalars(select(ToolCall).where(ToolCall.task_id == task.id))
        )
        steps = list(
            session.scalars(
                select(Step).where(Step.task_id == task.id).order_by(Step.id)
            )
        )

        assert task.status == "step_limit"
        assert len(tool_calls) == chat.MAX_STEPS
        assert [step.step_index for step in steps] == list(range(chat.MAX_STEPS + 1))
        assert all(step.status == "completed" for step in steps[:-1])
        assert steps[-1].status == "step_limit"
        terminal = json.loads(steps[-1].detail)
        assert terminal["stop_reason"] == "step_limit"
        assert terminal["observation"] is None
