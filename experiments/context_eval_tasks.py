"""Tám kịch bản synthetic cố định để so sánh policy context Sprint 3."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    turns: tuple[str, ...]
    expected: tuple[str, ...]
    forbidden: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    required_associations: tuple[tuple[str, str], ...] = ()


def _filler(index: int) -> str:
    return f"Nhiễu {index}: " + ("nội dung không liên quan " * 55)


CONTEXT_EVAL_CASES = (
    EvalCase(
        "E-01",
        ("Ghi nhớ mã FACT_E01_ALPHA.", *(_filler(i) for i in range(10)), "Mã đã lưu là gì?"),
        ("FACT_E01_ALPHA",),
    ),
    EvalCase(
        "E-02",
        (
            "Quyết định ban đầu là FACT_E02_OLD.",
            *(_filler(i) for i in range(4)),
            "Cập nhật quyết định thành FACT_E02_NEW vì REASON_E02.",
            *(_filler(i) for i in range(7, 13)),
            "Quyết định hiện tại và lý do là gì?",
        ),
        ("FACT_E02_NEW", "REASON_E02"),
        ("FACT_E02_OLD",),
    ),
    EvalCase(
        "E-03",
        (
            "Nguồn A ghi FACT_E03_A.",
            "Nguồn B ghi FACT_E03_B.",
            *(_filler(i) for i in range(10)),
            "Nêu đúng dữ liệu của cả nguồn A và B?",
        ),
        ("FACT_E03_A", "FACT_E03_B"),
        required_associations=(
            ("Nguồn A", "FACT_E03_A"),
            ("Nguồn B", "FACT_E03_B"),
        ),
    ),
    EvalCase(
        "E-04",
        ("TOOL: e04.txt",),
        ("FACT_E04_HEAD", "FACT_E04_TAIL", "e04.txt"),
        files={"e04.txt": "FACT_E04_HEAD\n" + "x" * 4900 + "\nFACT_E04_TAIL"},
    ),
    EvalCase(
        "E-05",
        ("TOOL: e05.txt",),
        ("FACT_E05_MIDDLE", "e05.txt"),
        files={"e05.txt": "h" * 2450 + "FACT_E05_MIDDLE" + "t" * 2450},
    ),
    EvalCase(
        "E-06",
        (
            "TOOL: e06.txt",
            *(_filler(i) for i in range(11)),
            "Kết quả công cụ trước đó và nguồn là gì?",
        ),
        ("FACT_E06_TOOL", "e06.txt"),
        files={"e06.txt": "FACT_E06_TOOL"},
    ),
    EvalCase(
        "E-07",
        (
            "TOOL: missing.txt",
            "TOOL: e07.txt",
            *(_filler(i) for i in range(9)),
            "Kết quả hợp lệ mới nhất và nguồn là gì?",
        ),
        ("FACT_E07_RECOVERED", "e07.txt"),
        ("missing.txt",),
        files={"e07.txt": "FACT_E07_RECOVERED"},
    ),
    EvalCase(
        "E-08",
        (
            "Quyết định FACT_E08_OLD vì REASON_E08_OLD.",
            *(_filler(i) for i in range(8)),
            "Đổi thành FACT_E08_NEW vì REASON_E08_NEW, nguồn SOURCE_E08.",
            *(_filler(i) for i in range(11, 21)),
            "Quyết định hiện tại, lý do và nguồn là gì?",
        ),
        ("FACT_E08_NEW", "REASON_E08_NEW", "SOURCE_E08"),
        ("FACT_E08_OLD", "REASON_E08_OLD"),
    ),
)
