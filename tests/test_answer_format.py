"""Five-part task reply rendering: deterministic, bounded, plain text."""

from laplace.services.answer_format import (
    PendingAction,
    event_limitation,
    render_task_reply,
    stop_text,
    stored_task_message,
)
from laplace.services.verifier import Check, VerificationReport


def _report(status="verified", checks=(), sources=(), limitations=()):
    return VerificationReport(status, tuple(checks), tuple(sources), tuple(limitations))


def _sections(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith(("-", "["))]


def test_sections_follow_the_fixed_order_and_show_verification_status() -> None:
    report = _report(
        "partial",
        checks=[
            Check("effect", True, "Reminder #12 có trong hệ thống (pending, 2026-10-25T01:00:00Z)",
                  "reminder:12"),
            Check("citation", False, "URL trong câu trả lời không thuộc nguồn đã đọc", "https://x"),
        ],
        sources=["https://example.test/a"],
        limitations=["Chưa có trích dẫn để kiểm chứng nội dung trả lời."],
    )
    text = render_task_reply(result="Đã xong.", report=report, limitations=["Giả định múi giờ VN."])
    assert _sections(text) == [
        "Kết quả", "Đã xong.", "Căn cứ đã kiểm tra — Kiểm chứng một phần", "Nguồn", "Giới hạn",
        "Hành động còn chờ duyệt",
    ]
    assert "- Đạt: Reminder #12 có trong hệ thống" in text
    assert "- Không đạt: URL trong câu trả lời" in text
    assert "[1] https://example.test/a" in text
    assert "- Giả định múi giờ VN." in text and "- Chưa có trích dẫn" in text
    assert text.endswith("Hành động còn chờ duyệt\n- Không có")


def test_empty_sections_say_none_and_sources_are_omitted() -> None:
    text = render_task_reply(result="Chat", report=_report("not_applicable"))
    assert "Căn cứ đã kiểm tra — Không áp dụng\n- Không có" in text
    assert "Nguồn" not in text
    assert "Giới hạn\n- Không có" in text
    assert render_task_reply(result="x", report=None).count("Không áp dụng") == 1


def test_pending_action_shows_preview_line_and_minutes() -> None:
    text = render_task_reply(
        result="Đang chờ.", report=_report("not_applicable"),
        pending=PendingAction("Operation: create", 899),
    )
    assert text.endswith("Hành động còn chờ duyệt\n- Operation: create (hết hạn sau 15 phút)")
    assert PendingAction("x", 0).render().endswith("1 phút)")


def test_bounds_checks_sources_and_limitations() -> None:
    checks = [Check("tool", True, "c" * 400, "t")] * 12
    report = _report("verified", checks=checks, sources=[f"https://s/{i}" for i in range(9)])
    text = render_task_reply(
        result="r", report=report, limitations=[f"lim {i} " + "x" * 300 for i in range(10)]
    )
    assert text.count("- Đạt:") == 8
    assert text.count("\n[") == 5
    assert text.count("- lim ") == 6
    assert max(len(line) for line in text.splitlines()) < 520


def test_stop_texts_and_event_limitations_cover_new_reasons() -> None:
    for reason in ("param_error", "permission_required", "service_error", "restart_interrupted"):
        assert stop_text(reason) != stop_text("unknown")
        assert event_limitation(reason).startswith("Lượt dừng sớm: ")
    assert event_limitation("final") is None
    assert "/remind" in stop_text("permission_required")


def test_stored_message_keeps_result_and_effect_facts_only() -> None:
    report = _report(
        "verified",
        checks=[Check("effect", True, "Reminder #3 có trong hệ thống (pending, t)", "reminder:3"),
                Check("tool", True, "Công cụ web_search trả kết quả hợp lệ (1 lần)", "web_search")],
    )
    stored = stored_task_message("Xong.", report)
    assert stored == "Thao tác đã thực thi:\n- Reminder #3 có trong hệ thống (pending, t)\n\nXong."
    assert stored_task_message("Xong.", _report("verified")) == "Xong."
    assert stored_task_message("Xong.", None) == "Xong."


def test_failing_checks_are_listed_before_passing_ones_when_truncated() -> None:
    checks = [Check("tool", True, f"ok {i}", "t") for i in range(9)]
    checks.append(Check("citation", False, "URL trong câu trả lời không thuộc nguồn đã đọc", "u"))
    text = render_task_reply(result="r", report=_report("partial", checks=checks))
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert lines[0].startswith("- Không đạt:") and text.count("- Đạt:") == 7
