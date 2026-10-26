"""``python -m laplace.evals`` exit codes and run-id handling."""

import pytest

from laplace.config import Settings
from laplace.db import reset_engine_for_tests
from laplace.evals.__main__ import main
from laplace.services import approvals, chat, memory


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        approvals, "Settings", lambda: Settings(_env_file=None, approval_timeout_seconds=900)
    )
    monkeypatch.setattr(
        chat, "Settings",
        lambda: Settings(_env_file=None, context_max_chars=12_000, timezone="UTC", max_steps=5),
    )
    yield
    memory.start_chat_admission()
    reset_engine_for_tests(None)


def test_exit_codes_and_run_id_rules(tmp_path, capsys) -> None:
    base = ["--provider", "mock", "--out", str(tmp_path)]
    assert main([*base, "--only", "n01-greeting", "--run-id", "ok"]) == 0
    assert (tmp_path / "ok" / "results.json").is_file()
    assert main([*base, "--only", "n01-greeting", "--run-id", "ok"]) == 2  # reuse refused
    assert "run already exists" in capsys.readouterr().err
    assert main([*base, "--only", "nope-01"]) == 2
    assert main([*base, "--run-id", "../escape"]) == 2
    assert main([*base, "--cases", str(tmp_path / "empty")]) == 2
