"""Kiểm thử toàn tuyến lượt chat bằng MockLLM: không mạng, không cần API key.

Phủ các contract Sprint 2: trả lời trực tiếp, gọi tool có validate tham số,
self-correction khi LLM trả sai schema, ghi ``llm_calls`` và tính usage theo
người dùng.
"""

import pytest

from laplace import repo
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.mock import MockLLM
from laplace.models import User
from laplace.services import chat
from laplace.services.chat import handle_message
from laplace.tools.base import execute, load_builtin_tools


@pytest.fixture(autouse=True)
def _memory_db():
    """Mỗi test chạy trên SQLite in-memory sạch rồi trả engine về mặc định."""
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
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


def test_schema_failure_gives_honest_reply(monkeypatch):
    """Sai schema quá số lần cho phép: lượt dừng với lời giải thích trung thực."""
    _install_mock(monkeypatch, [{"action": "tool"}] * (chat.MAX_SCHEMA_RETRIES + 1))
    reply = handle_message(444, None, "hỏi")
    assert "không hợp lệ" in reply.text


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
