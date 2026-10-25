"""Evidence-only verification of one task: reads DB rows, never the model's word.

Checks: reminder receipts against their rows, every used tool against at least one
ok call, proposed quotes against the stored source text, and cited URLs against
observed sources. Read-only, no LLM, no network, one short session.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from laplace import repo
from laplace.db import session_scope
from laplace.services.reminders import public_reminder
from laplace.tools.errors import error_label

CheckKind = Literal["effect", "tool", "source", "citation"]
VerificationStatus = Literal["verified", "partial", "unverified", "not_applicable"]

MAX_CLAIMS = 3
MAX_CHECKS = 8
MAX_SOURCES_SHOWN = 5
MIN_QUOTE_CHARS = 8
QUOTE_LABEL_CHARS = 120
_URL = re.compile(r"https?://[^\s<>\"'\[\]]+")
_LATER_LIFECYCLE = frozenset({"delivering", "sent"})


@dataclass(frozen=True, slots=True)
class Claim:
    source: str
    quote: str


@dataclass(frozen=True, slots=True)
class Receipt:
    """Committed reminder effect as returned by the approved tool (ReminderOutput)."""

    tool_name: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Check:
    kind: CheckKind
    ok: bool
    label: str
    ref: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: VerificationStatus
    checks: tuple[Check, ...]
    sources: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_json(self) -> str:
        """Durable projection: kind/ok/ref only; no quotes, params or page content."""
        return json.dumps(
            {
                "version": 1,
                "checks": [
                    {"kind": check.kind, "ok": check.ok, "ref": check.ref[:200]}
                    for check in self.checks
                ],
            },
            ensure_ascii=False,
        )


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _short_quote(quote: str) -> str:
    compact = " ".join(quote.split())
    return compact if len(compact) <= QUOTE_LABEL_CHARS else compact[: QUOTE_LABEL_CHARS - 1] + "…"


def _source_text(row: Mapping[str, Any], source: str) -> str | None:
    """Stored text behind one source of an ok tool call, for the known tool shapes."""
    data = row.get("data")
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if isinstance(results, list):
        parts = [
            str(item.get("content", ""))
            for item in results
            if isinstance(item, dict) and item.get("url") == source
        ]
        return "\n".join(parts) if parts else None
    document = data.get("document")
    units = data.get("units")
    if isinstance(document, dict) and isinstance(units, list):
        attachment_id = document.get("attachment_id")
        for unit in units:
            if not isinstance(unit, dict):
                continue
            if f"attachment:{attachment_id}#{unit.get('locator')}" == source:
                return str(unit.get("text", ""))
    return None


def _effect_check(session, user_id: int, receipt: Receipt) -> Check:
    data = receipt.data
    reminder_id = data.get("id")
    ref = f"reminder:{reminder_id}"
    if type(reminder_id) is not int:
        return Check("effect", False, "Receipt reminder không có ID hợp lệ", ref)
    row = repo.reminder_for_owner(session, user_id, reminder_id)
    if row is None:
        return Check(
            "effect", False, f"Reminder #{reminder_id} không có trong hệ thống của bạn", ref
        )
    current = public_reminder(row)
    expected_status = "cancelled" if receipt.tool_name == "cancel_reminder" else data.get("status")
    status_ok = current["status"] == expected_status or (
        receipt.tool_name != "cancel_reminder" and current["status"] in _LATER_LIFECYCLE
    )
    content_ok = receipt.tool_name == "cancel_reminder" or (
        current["message"] == data.get("message") and current["run_at"] == data.get("run_at")
    )
    if status_ok and content_ok:
        return Check(
            "effect", True,
            f"Reminder #{reminder_id} có trong hệ thống ({current['status']}, {current['run_at']})",
            ref,
        )
    return Check("effect", False, f"Reminder #{reminder_id} không khớp dữ liệu đã duyệt", ref)


def _tool_checks(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Check], list[str]]:
    checks: list[Check] = []
    limitations: list[str] = []
    for name in dict.fromkeys(str(row["tool_name"]) for row in rows):
        calls = [row for row in rows if row["tool_name"] == name]
        ok_count = sum(1 for row in calls if row["ok"])
        last = calls[-1]
        if ok_count:
            checks.append(
                Check("tool", True, f"Công cụ {name} trả kết quả hợp lệ ({ok_count} lần)", name)
            )
            if not last["ok"]:
                limitations.append(
                    f"Lần gọi cuối của {name} lỗi ({error_label(last.get('error_code'))})."
                )
        else:
            label = error_label(last.get("error_code"))
            checks.append(Check("tool", False, f"Công cụ {name} lỗi: {label}", name))
    return checks, limitations


def _source_checks(
    rows: Sequence[Mapping[str, Any]], claims: Sequence[Claim], numbering: dict[str, int]
) -> list[Check]:
    checks: list[Check] = []
    for claim in claims[:MAX_CLAIMS]:
        short = _short_quote(claim.quote)
        index = numbering.get(claim.source)
        if index is None:
            label = f"\"{short}\" dẫn nguồn không thuộc nguồn đã đọc"
            checks.append(Check("source", False, label, claim.source))
            continue
        if len(claim.quote.strip()) < MIN_QUOTE_CHARS:
            label = f"\"{short}\" quá ngắn để kiểm chứng"
            checks.append(Check("source", False, label, claim.source))
            continue
        texts = [
            text for row in rows if row["ok"] and claim.source in row["sources"]
            for text in (_source_text(row, claim.source),) if text is not None
        ]
        found = any(normalize_text(claim.quote) in normalize_text(text) for text in texts)
        checks.append(
            Check(
                "source", found,
                f"\"{short}\" {'có trong' if found else 'không tìm thấy trong'} nguồn [{index}]",
                claim.source,
            )
        )
    return checks


def _clean_url(url: str) -> str:
    """Trim sentence punctuation; keep a closing parenthesis only when it is balanced."""
    cleaned = url.rstrip(".,;:!?\"'")
    while cleaned.endswith(")") and cleaned.count(")") > cleaned.count("("):
        cleaned = cleaned[:-1].rstrip(".,;:!?\"'")
    return cleaned


def _same_source(url: str, source: str) -> bool:
    return url.rstrip("/") == source.rstrip("/")


def _citation_checks(answer_text: str, observed: set[str]) -> list[Check]:
    checks: list[Check] = []
    cited = dict.fromkeys(_clean_url(url) for url in _URL.findall(answer_text or ""))
    for cleaned in cited:
        ok = any(_same_source(cleaned, source) for source in observed)
        label = (
            "URL trong câu trả lời thuộc nguồn đã đọc" if ok
            else "URL trong câu trả lời không thuộc nguồn đã đọc"
        )
        checks.append(Check("citation", ok, label, cleaned))
        if len(checks) >= MAX_CHECKS:
            break
    return checks


def aggregate(checks: Sequence[Check], *, sourced_read_without_claims: bool) -> VerificationStatus:
    if not checks:
        return "not_applicable"
    if any(check.kind == "effect" and not check.ok for check in checks):
        return "unverified"
    passed = sum(1 for check in checks if check.ok)
    if passed == 0:
        return "unverified"
    if passed == len(checks):
        return "partial" if sourced_read_without_claims else "verified"
    return "partial"


def _fallback_report(receipts: Sequence[Receipt]) -> VerificationReport:
    checks = tuple(
        Check(
            "effect", False,
            f"Reminder #{receipt.data.get('id')} chưa đối chiếu được với hệ thống",
            f"reminder:{receipt.data.get('id')}",
        )
        for receipt in receipts
    )
    return VerificationReport(
        "unverified", checks, (), ("Không đọc được dữ liệu để kiểm chứng kết quả.",)
    )


def verify_task(
    *,
    task_id: int,
    user_id: int,
    receipts: Sequence[Receipt] = (),
    claims: Sequence[Claim] = (),
    answer_text: str = "",
    timeout_seconds: float | None = None,
) -> VerificationReport:
    """Build the report from persisted evidence; a DB failure yields ``unverified``."""
    try:
        with session_scope(timeout_seconds=timeout_seconds) as session:
            rows = repo.task_tool_results(session, task_id)
            effect_checks = [_effect_check(session, user_id, receipt) for receipt in receipts]
    except Exception:
        return _fallback_report(receipts)
    tool_checks, limitations = _tool_checks(rows)
    observed = list(dict.fromkeys(
        source for row in rows if row["ok"] for source in row["sources"]
    ))
    claimed = [claim.source for claim in claims[:MAX_CLAIMS] if claim.source in observed]
    ordered = list(dict.fromkeys([*claimed, *observed]))
    numbering = {source: index for index, source in enumerate(ordered, start=1)}
    source_checks = _source_checks(rows, claims, numbering)
    citation_checks = _citation_checks(answer_text, set(observed))
    checks = [*effect_checks, *tool_checks, *source_checks, *citation_checks]
    sourced_read_without_claims = bool(observed) and not claims and not effect_checks
    if sourced_read_without_claims:
        limitations.append("Chưa có trích dẫn để kiểm chứng nội dung trả lời.")
    status = aggregate(checks, sourced_read_without_claims=sourced_read_without_claims)
    return VerificationReport(
        status, tuple(checks), tuple(ordered[:MAX_SOURCES_SHOWN]), tuple(limitations)
    )
