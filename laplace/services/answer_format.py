"""Plain-text five-part reply for every task turn: result, evidence, sources, limits, pending.

The runtime builds the evidence/sources/pending sections from the verifier and the
approval state; the model only supplies the result text, quotes and limitations.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from laplace.services.verifier import MAX_CHECKS, MAX_SOURCES_SHOWN, VerificationReport

MAX_LIMITATIONS = 6
LIMITATION_CHARS = 200

STATUS_LABELS: dict[str, str] = {
    "verified": "Đã kiểm chứng",
    "partial": "Kiểm chứng một phần",
    "unverified": "Chưa kiểm chứng",
    "not_applicable": "Không áp dụng",
}

_STOP_TEXTS: dict[str, str] = {
    "step_limit": "Đã dùng hết số bước công cụ được phép; lượt này dừng.",
    "time_limit": (
        "Đã hết thời gian xử lý cho lượt này "
        "không bắt đầu thêm yêu cầu hoặc thao tác."
    ),
    "cost_limit": (
        "Đã chạm ngân sách chi phí của lượt này "
        "không bắt đầu thêm yêu cầu hoặc thao tác."
    ),
    "budget_unavailable": "Không thể xác định an toàn ngân sách của yêu cầu; lượt này dừng.",
    "repeated_error": "Một lỗi công cụ lặp lại quá số lần cho phép; lượt này dừng.",
    "cancelled": "Lượt xử lý đã được hủy.",
    "param_error": (
        "Công cụ từ chối tham số hai lần liên tiếp; lượt này dừng để tránh lặp lỗi."
    ),
    "permission_required": (
        "Thao tác này cần quyền mà yêu cầu hiện tại không có. Với reminder, hãy dùng "
        "/remind, /remind_update hoặc /remind_cancel trong chat riêng với bot."
    ),
    "service_error": (
        "Một dịch vụ công cụ gặp lỗi không thể tự khắc phục; lượt này dừng "
        "và không thử lại thao tác."
    ),
    "restart_interrupted": (
        "Lượt xử lý trước bị gián đoạn do bot khởi động lại; hệ thống không tự chạy lại."
    ),
}

# Short Vietnamese reason shown under Giới hạn for every non-final terminal event.
EVENT_LABELS: dict[str, str] = {
    "step_limit": "hết số bước công cụ",
    "time_limit": "hết thời gian xử lý",
    "cost_limit": "chạm ngân sách chi phí",
    "budget_unavailable": "không xác định được ngân sách",
    "repeated_error": "lỗi công cụ lặp lại",
    "cancelled": "người dùng hủy",
    "schema_failure": "mô hình trả dữ liệu không hợp lệ",
    "context_overflow": "vượt ngân sách context",
    "execution_error": "lỗi thực thi",
    "effect_incomplete": "chưa hoàn tất thao tác reminder bắt buộc",
    "approval_invalid": "không tạo được bản xác nhận",
    "approval_invalidated": "đề xuất đã duyệt không còn hợp lệ",
    "approval_failed": "thao tác đã duyệt thất bại",
    "approval_cancelled": "đề xuất bị hủy",
    "param_error": "lỗi đầu vào lặp lại",
    "permission_required": "thiếu quyền",
    "service_error": "lỗi dịch vụ",
    "restart_interrupted": "bot khởi động lại giữa lượt",
}


def stop_text(reason: str) -> str:
    return _STOP_TEXTS.get(reason, "Lượt xử lý đã dừng an toàn.")


def event_limitation(event: str) -> str | None:
    label = EVENT_LABELS.get(event)
    return None if label is None else f"Lượt dừng sớm: {label}."


@dataclass(frozen=True, slots=True)
class PendingAction:
    preview_line: str
    expires_in_seconds: int

    def render(self) -> str:
        minutes = max(1, math.ceil(self.expires_in_seconds / 60))
        return f"{self.preview_line} (hết hạn sau {minutes} phút)"


def _bounded(text: str, limit: int) -> str:
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def render_task_reply(
    *,
    result: str,
    report: VerificationReport | None,
    limitations: Sequence[str] = (),
    pending: PendingAction | None = None,
) -> str:
    """Deterministic plain text (no Markdown, no emoji); callers split for Telegram."""
    status = report.status if report is not None else "not_applicable"
    lines = ["Kết quả", result.strip() or "(không có nội dung)", ""]
    lines.append(f"Căn cứ đã kiểm tra — {STATUS_LABELS[status]}")
    # Failing checks are shown first so truncation never hides the reason for "partial".
    checks = (
        sorted(report.checks, key=lambda check: check.ok)[:MAX_CHECKS]
        if report is not None else ()
    )
    if checks:
        lines.extend(
            f"- {'Đạt' if check.ok else 'Không đạt'}: {_bounded(check.label, 220)}"
            for check in checks
        )
    else:
        lines.append("- Không có")
    sources = report.sources[:MAX_SOURCES_SHOWN] if report is not None else ()
    if sources:
        lines.append("")
        lines.append("Nguồn")
        lines.extend(
            f"[{index}] {_bounded(source, 500)}" for index, source in enumerate(sources, 1)
        )
    merged: list[str] = []
    for item in (*limitations, *(report.limitations if report is not None else ())):
        text = _bounded(item, LIMITATION_CHARS)
        if text and text not in merged:
            merged.append(text)
    lines.append("")
    lines.append("Giới hạn")
    if merged:
        lines.extend(f"- {item}" for item in merged[:MAX_LIMITATIONS])
    else:
        lines.append("- Không có")
    lines.append("")
    lines.append("Hành động còn chờ duyệt")
    lines.append(f"- {_bounded(pending.render(), 300)}" if pending is not None else "- Không có")
    return "\n".join(lines)


def stored_task_message(result: str, report: VerificationReport | None) -> str:
    """Conversation-log form: core result plus committed effect facts, no boilerplate."""
    checks = report.checks if report is not None else ()
    effects = [check.label for check in checks if check.kind == "effect"]
    if not effects:
        return result
    lines = "\n".join(f"- {label}" for label in effects)
    return f"Thao tác đã thực thi:\n{lines}\n\n{result}"
