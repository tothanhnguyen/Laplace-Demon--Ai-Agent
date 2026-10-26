"""Declarative evaluation cases: strict schema plus a YAML loader.

``$seeded_reminder_id`` in the input text or mock script is replaced with the id of
the first reminder seeded by ``initial_state`` so cases stay order-independent.

A case states the starting situation, the input, the model script used in mock
mode, injected tool faults, the approval decision, the expected observable
outcome and the forbidden actions. Expectations describe behaviour visible in
the database or the reply, never model wording.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"
GROUPS = ("normal", "clarify", "tool_error", "injection", "approval")
Group = Literal["normal", "clarify", "tool_error", "injection", "approval"]
FaultStep = Literal[
    "ok", "timeout", "invalid_input", "not_found", "forbidden", "runtime_error", "upstream_error"
]
_CASE_ID = re.compile(r"^[a-z]\d{2}-[a-z0-9-]{1,48}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CaseInput(_Strict):
    text: str = Field(min_length=1, max_length=4_000)
    transport: Literal["telegram", "cli"] = "telegram"
    # attachment name -> file under <cases>/fixtures; resolved to absolute paths on load
    attachments: dict[str, str] = Field(default_factory=dict)


class SeedMessage(_Strict):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)


class SeedReminder(_Strict):
    message: str = Field(min_length=1, max_length=500)
    delay_seconds: int = Field(ge=60, le=365 * 86_400)


class InitialState(_Strict):
    messages: list[SeedMessage] = Field(default_factory=list)
    reminders: list[SeedReminder] = Field(default_factory=list)


class Fault(_Strict):
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    script: list[FaultStep] = Field(min_length=1, max_length=10)


class Expected(_Strict):
    # "none" means no task row was created (plain chat).
    task_status: list[str] | None = None
    stop_reason: str | None = None
    verification_status: list[str] | None = None
    reply_contains: list[str] = Field(default_factory=list)
    reply_contains_any: list[str] = Field(default_factory=list)
    reply_not_contains: list[str] = Field(default_factory=list)
    # Number of the user's reminder rows that are not cancelled.
    reminders: int | None = Field(default=None, ge=0)
    tool_calls_min: int = Field(default=0, ge=0)
    # Tools that must have at least one successful call on the task.
    tool_ok: list[str] = Field(default_factory=list)
    approval_status: str | None = None

    @field_validator("task_status", "verification_status", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class Forbidden(_Strict):
    tools: list[str] = Field(default_factory=list)
    write_without_approval: bool = True
    reply_patterns: list[str] = Field(default_factory=list)

    @field_validator("reply_patterns")
    @classmethod
    def _compiles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from None
        return value


class Budget(_Strict):
    max_steps: int = Field(default=5, ge=1, le=20)
    max_seconds: float = Field(default=120.0, ge=1, le=600)


class EvalCase(_Strict):
    id: str
    group: Group
    description: str = Field(min_length=1, max_length=500)
    input: CaseInput
    initial_state: InitialState = Field(default_factory=InitialState)
    mock_script: list[dict[str, Any] | str] = Field(default_factory=list)
    faults: list[Fault] = Field(default_factory=list)
    # Canned page text for a network tool (web_search/fetch_page) in every mode.
    stubs: dict[str, str] = Field(default_factory=dict)
    approval: Literal["none", "approve", "reject"] = "none"
    expected: Expected = Field(default_factory=Expected)
    forbidden: Forbidden = Field(default_factory=Forbidden)
    budget: Budget = Field(default_factory=Budget)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _CASE_ID.match(value):
            raise ValueError("case id must look like n01-short-slug")
        return value


class CaseLoadError(ValueError):
    """A case file is malformed; the message names the file."""


def load_case(path: Path) -> EvalCase:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level must be a mapping")
        case = EvalCase.model_validate(raw)
    except (yaml.YAMLError, ValueError) as exc:
        raise CaseLoadError(f"{path.name}: {exc}") from None
    fixtures = path.parent / "fixtures"
    resolved: dict[str, str] = {}
    for name, relative in case.input.attachments.items():
        target = (fixtures / relative).resolve()
        if not target.is_file() or fixtures.resolve() not in target.parents:
            raise CaseLoadError(f"{path.name}: attachment {name!r} missing under fixtures/")
        resolved[name] = str(target)
    case.input.attachments = resolved
    return case


def load_cases(directory: Path = DEFAULT_CASES_DIR) -> list[EvalCase]:
    """Load every ``*.yaml`` in ``directory`` sorted by file name; ids must be unique."""
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        case = load_case(path)
        if case.id in seen:
            raise CaseLoadError(f"{path.name}: duplicate case id {case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases
