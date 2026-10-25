"""Verifier reads persisted evidence only: rows, tool envelopes, stored source text."""

import json

import pytest

from laplace import repo
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.services import verifier
from laplace.services.verifier import Claim, Receipt, aggregate, normalize_text, verify_task

OWNER = 51
OTHER = 52
URL = "https://example.test/python-314"
PAGE = "Python 3.14 được phát hành vào tháng 10 năm 2025 với free-threading chính thức."


@pytest.fixture(autouse=True)
def _db() -> None:
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_tests(None)


def _task(telegram_user_id: int = OWNER) -> tuple[int, int]:
    with session_scope() as session:
        user = repo.get_or_create_user(session, telegram_user_id, None)
        conversation = repo.get_or_create_conversation(session, user)
        request = repo.add_message(session, conversation, "user", "goal")
        task = repo.create_task(
            session, user_id=user.id, conversation_id=conversation.id,
            request_message_id=request.id, goal="goal",
        )
        return task.id, user.id


def _record(task_id: int, tool_name: str, *, ok: bool, data=None, sources=(), error_code=None):
    envelope = (
        {"ok": True, "data": data, "sources": list(sources), "latency_ms": 1}
        if ok
        else {"ok": False, "error": "synthetic", "error_code": error_code or "upstream_error",
              "retryable": False, "sources": [], "latency_ms": 1}
    )
    with session_scope() as session:
        repo.record_tool_call(
            session, task_id=task_id, tool_name=tool_name, params_json="{}",
            result_json=json.dumps(envelope, ensure_ascii=False), ok=ok,
            error=None if ok else "synthetic", latency_ms=1,
        )


def _reminder(user_id: int, *, message="Kiểm tra E2E", due=1_900_000_000) -> dict:
    with session_scope() as session:
        row = repo.create_reminder(
            session, user_id=user_id, message=message, timezone="UTC", due_at_utc=due
        )
        from laplace.services.reminders import public_reminder

        return public_reminder(row)


def _web_rows(task_id: int, content: str = PAGE, url: str = URL) -> None:
    _record(
        task_id, "web_search", ok=True, sources=(url,),
        data={"query": "python", "results": [{"title": "t", "url": url, "content": content}]},
    )


def test_created_reminder_matching_row_is_verified() -> None:
    task_id, user_id = _task()
    receipt = _reminder(user_id)
    report = verify_task(
        task_id=task_id, user_id=user_id, receipts=[Receipt("create_reminder", receipt)]
    )
    assert report.status == "verified"
    assert [check.kind for check in report.checks] == ["effect"]
    assert report.checks[0].ok and f"#{receipt['id']}" in report.checks[0].label
    assert json.loads(report.to_json())["checks"] == [
        {"kind": "effect", "ok": True, "ref": f"reminder:{receipt['id']}"}
    ]


@pytest.mark.parametrize("variant", ["deleted", "other_owner", "edited"])
def test_receipt_without_matching_row_is_unverified(variant) -> None:
    task_id, user_id = _task()
    receipt = _reminder(user_id)
    if variant == "deleted":
        with session_scope() as session:
            from laplace.models import Reminder

            session.delete(session.get(Reminder, receipt["id"]))
    elif variant == "other_owner":
        _, user_id = _task(OTHER)
    else:
        with session_scope() as session:
            from laplace.models import Reminder

            session.get(Reminder, receipt["id"]).message = "tampered"
    report = verify_task(
        task_id=task_id, user_id=user_id, receipts=[Receipt("create_reminder", receipt)]
    )
    assert report.status == "unverified"
    assert not report.checks[0].ok


def test_cancel_receipt_requires_cancelled_row() -> None:
    task_id, user_id = _task()
    receipt = _reminder(user_id)
    report = verify_task(
        task_id=task_id, user_id=user_id,
        receipts=[Receipt("cancel_reminder", {**receipt, "status": "cancelled"})],
    )
    assert report.status == "unverified"
    with session_scope() as session:
        from laplace.models import Reminder

        session.get(Reminder, receipt["id"]).status = "cancelled"
    report = verify_task(
        task_id=task_id, user_id=user_id,
        receipts=[Receipt("cancel_reminder", {**receipt, "status": "cancelled"})],
    )
    assert report.status == "verified"


def test_sent_reminder_still_matches_create_receipt() -> None:
    task_id, user_id = _task()
    receipt = _reminder(user_id)
    with session_scope() as session:
        from laplace.models import Reminder

        session.get(Reminder, receipt["id"]).status = "sent"
    report = verify_task(
        task_id=task_id, user_id=user_id, receipts=[Receipt("create_reminder", receipt)]
    )
    assert report.status == "verified" and "sent" in report.checks[0].label


def test_quote_found_in_stored_web_content_is_verified_with_numbered_source() -> None:
    task_id, user_id = _task()
    _web_rows(task_id)
    report = verify_task(
        task_id=task_id, user_id=user_id,
        claims=[Claim(URL, "phát hành vào tháng 10 năm 2025")],
        answer_text=f"Theo {URL}, 3.14 ra tháng 10/2025.",
    )
    assert report.status == "verified"
    assert report.sources == (URL,)
    kinds = [(check.kind, check.ok) for check in report.checks]
    assert kinds == [("tool", True), ("source", True), ("citation", True)]
    assert "nguồn [1]" in report.checks[1].label


def test_quote_matching_ignores_case_whitespace_and_unicode_form() -> None:
    import unicodedata

    task_id, user_id = _task()
    _web_rows(task_id, content="  PHÁT   hành\nvào tháng 10 ")
    decomposed = unicodedata.normalize("NFD", "phát hành vào tháng 10")
    report = verify_task(task_id=task_id, user_id=user_id, claims=[Claim(URL, decomposed)])
    assert report.status == "verified"
    assert normalize_text("Ê  x") == normalize_text("ê x")


def test_missing_quote_or_foreign_source_is_not_verified() -> None:
    task_id, user_id = _task()
    _web_rows(task_id)
    report = verify_task(
        task_id=task_id, user_id=user_id, claims=[Claim(URL, "điều này không có trong trang")]
    )
    assert report.status == "partial"
    assert [check.ok for check in report.checks] == [True, False]
    report = verify_task(
        task_id=task_id, user_id=user_id,
        claims=[Claim("https://elsewhere.test/x", "phát hành vào tháng 10")],
    )
    assert report.status == "partial"
    assert "không thuộc nguồn đã đọc" in report.checks[1].label


def test_fabricated_url_in_answer_fails_citation_check() -> None:
    task_id, user_id = _task()
    _web_rows(task_id)
    report = verify_task(
        task_id=task_id, user_id=user_id,
        claims=[Claim(URL, "phát hành vào tháng 10 năm 2025")],
        answer_text="Xem https://fake.example/evidence.",
    )
    assert report.status == "partial"
    citation = [check for check in report.checks if check.kind == "citation"][0]
    assert not citation.ok and citation.ref == "https://fake.example/evidence"


def test_document_locator_source_text_is_checked() -> None:
    task_id, user_id = _task()
    source = "attachment:doc-1#line:3"
    _record(
        task_id, "read_document", ok=True, sources=(source,),
        data={"document": {"attachment_id": "doc-1"}, "units": [
            {"unit": 3, "locator": "line:3", "text": "Hạn chót nộp báo cáo là 20/10."},
        ], "next_unit": None, "complete": True, "limitations": []},
    )
    report = verify_task(
        task_id=task_id, user_id=user_id, claims=[Claim(source, "nộp báo cáo là 20/10")]
    )
    assert report.status == "verified"


def test_read_without_claims_is_partial_with_limitation() -> None:
    task_id, user_id = _task()
    _web_rows(task_id)
    report = verify_task(task_id=task_id, user_id=user_id)
    assert report.status == "partial"
    assert any("trích dẫn" in item for item in report.limitations)


def test_failed_tool_without_success_is_unverified_and_last_error_is_a_limitation() -> None:
    task_id, user_id = _task()
    _record(task_id, "fetch_page", ok=False, error_code="timeout")
    report = verify_task(task_id=task_id, user_id=user_id)
    assert report.status == "unverified"
    assert "hết thời gian chờ" in report.checks[0].label
    _web_rows(task_id)
    _record(task_id, "web_search", ok=False, error_code="upstream_error")
    report = verify_task(task_id=task_id, user_id=user_id)
    assert any("Lần gọi cuối của web_search" in item for item in report.limitations)


def test_no_evidence_is_not_applicable_and_db_failure_is_unverified(monkeypatch) -> None:
    task_id, user_id = _task()
    assert verify_task(task_id=task_id, user_id=user_id).status == "not_applicable"

    def broken(**_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(verifier, "session_scope", broken)
    report = verify_task(
        task_id=task_id, user_id=user_id,
        receipts=[Receipt("create_reminder", {"id": 7, "status": "pending"})],
    )
    assert report.status == "unverified"
    assert report.checks[0].ref == "reminder:7" and not report.checks[0].ok
    assert report.limitations


def test_report_json_never_contains_quotes_or_labels() -> None:
    task_id, user_id = _task()
    _web_rows(task_id)
    report = verify_task(
        task_id=task_id, user_id=user_id, claims=[Claim(URL, "phát hành vào tháng 10 năm 2025")]
    )
    payload = json.loads(report.to_json())
    assert set(payload) == {"version", "checks"}
    assert all(set(check) == {"kind", "ok", "ref"} for check in payload["checks"])
    assert "phát hành" not in report.to_json()


def test_aggregate_rules() -> None:
    from laplace.services.verifier import Check

    ok = Check("tool", True, "", "t")
    bad = Check("source", False, "", "s")
    effect_bad = Check("effect", False, "", "e")
    assert aggregate([], sourced_read_without_claims=False) == "not_applicable"
    assert aggregate([ok], sourced_read_without_claims=False) == "verified"
    assert aggregate([ok], sourced_read_without_claims=True) == "partial"
    assert aggregate([ok, bad], sourced_read_without_claims=False) == "partial"
    assert aggregate([bad], sourced_read_without_claims=False) == "unverified"
    assert aggregate([ok, effect_bad], sourced_read_without_claims=False) == "unverified"


def test_cited_url_with_parentheses_or_trailing_slash_matches_observed_source() -> None:
    task_id, user_id = _task()
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    _web_rows(task_id, url=wiki)
    _web_rows(task_id, url="https://docs.python.org/3/")
    report = verify_task(
        task_id=task_id, user_id=user_id,
        answer_text=f"Xem {wiki}. Và https://docs.python.org/3 nữa (nguồn: {wiki}).",
    )
    citations = [check for check in report.checks if check.kind == "citation"]
    assert [check.ok for check in citations] == [True, True]
    assert citations[0].ref == wiki
