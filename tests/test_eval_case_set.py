"""The bundled case set covers five groups and passes end to end in mock mode."""

from collections import Counter

import pytest

from laplace.config import Settings
from laplace.db import reset_engine_for_tests
from laplace.evals.cases import DEFAULT_CASES_DIR, load_cases
from laplace.evals.runner import run_suite
from laplace.services import approvals, chat, memory

EXPECTED_GROUPS = {"normal": 8, "clarify": 4, "tool_error": 3, "injection": 3, "approval": 2}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    from laplace.tools import errors
    from laplace.tools import reminders as reminder_tools

    monkeypatch.setattr(
        reminder_tools, "Settings", lambda: Settings(_env_file=None, reminder_max_horizon_days=365)
    )
    monkeypatch.setattr(errors, "sleep", lambda _s: None)
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    yield
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def test_case_set_shape() -> None:
    cases = load_cases(DEFAULT_CASES_DIR)
    assert Counter(case.group for case in cases) == EXPECTED_GROUPS
    assert all(case.forbidden.write_without_approval for case in cases)
    assert all(case.expected.model_dump(exclude_defaults=True) for case in cases)


def test_case_set_passes_in_mock_mode(tmp_path) -> None:
    cases = load_cases(DEFAULT_CASES_DIR)
    suite = run_suite(cases, provider_mode="mock", run_dir=tmp_path / "run", run_id="set")
    failures = {r.case.id: [c.detail for c in r.checks if not c.ok] for r in suite.results
                if not r.passed}
    assert not failures, failures
    assert suite.passed == 20
