"""Case schema and loader: strict, explicit errors, attachments resolved under fixtures/."""

import pytest

from laplace.evals.cases import DEFAULT_CASES_DIR, CaseLoadError, EvalCase, load_case, load_cases

MINIMAL = {
    "id": "n01-x", "group": "normal", "description": "d", "input": {"text": "hi"},
}


def test_minimal_case_gets_defaults() -> None:
    case = EvalCase.model_validate(MINIMAL)
    assert case.approval == "none" and case.budget.max_steps == 5
    assert case.forbidden.write_without_approval is True
    assert case.expected.task_status is None


def test_scalar_statuses_are_listified_and_ids_are_shaped() -> None:
    case = EvalCase.model_validate({**MINIMAL, "expected": {"task_status": "completed"}})
    assert case.expected.task_status == ["completed"]
    with pytest.raises(ValueError):
        EvalCase.model_validate({**MINIMAL, "id": "bad id"})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**MINIMAL, "unknown": 1})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**MINIMAL, "forbidden": {"reply_patterns": ["("]}})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**MINIMAL, "faults": [{"tool": "web_search", "script": ["boom"]}]})


def test_loader_reports_file_duplicates_and_missing_attachments(tmp_path) -> None:
    (tmp_path / "a.yaml").write_text("id: n01-a\ngroup: normal\ndescription: d\ninput: {text: x}\n")
    (tmp_path / "b.yaml").write_text("id: n01-a\ngroup: normal\ndescription: d\ninput: {text: x}\n")
    with pytest.raises(CaseLoadError, match="duplicate"):
        load_cases(tmp_path)
    (tmp_path / "b.yaml").write_text("- not a mapping\n")
    with pytest.raises(CaseLoadError, match="b.yaml"):
        load_cases(tmp_path)
    (tmp_path / "b.yaml").write_text(
        "id: n02-b\ngroup: normal\ndescription: d\n"
        "input: {text: x, attachments: {doc.txt: ../secret.txt}}\n"
    )
    with pytest.raises(CaseLoadError, match="attachment"):
        load_case(tmp_path / "b.yaml")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "note.txt").write_text("x")
    (tmp_path / "b.yaml").write_text(
        "id: n02-b\ngroup: normal\ndescription: d\n"
        "input: {text: x, attachments: {doc.txt: note.txt}}\n"
    )
    case = load_case(tmp_path / "b.yaml")
    assert case.input.attachments["doc.txt"].endswith("fixtures/note.txt")


def test_bundled_cases_load() -> None:
    cases = load_cases(DEFAULT_CASES_DIR)
    assert cases and all(case.group in ("normal", "clarify", "tool_error", "injection", "approval")
                         for case in cases)
