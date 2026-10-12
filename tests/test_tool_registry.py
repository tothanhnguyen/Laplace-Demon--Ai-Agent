"""Regression tests for the strict tool boundary and approval policy."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from laplace.config import Settings
from laplace.services.budget import TurnBudget
from laplace.tools import base
from laplace.tools.base import (
    ApprovalRequired,
    PreparedProposal,
    RiskLevel,
    ToolContext,
    ToolFailure,
    ToolModel,
    ToolPayload,
    execute,
    specs_for_llm,
    tool,
)


class _Args(ToolModel):
    query: str


class _CountArgs(ToolModel):
    count: int

    @field_validator("count")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("count must be positive")
        return value


class _CountOutput(ToolModel):
    count: int


class _Output(ToolModel):
    answer: str


class _Prepared(ToolModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    message: str
    user_id: int
    telegram_user_id: int


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})


@pytest.fixture
def tool_context(tmp_path) -> ToolContext:
    attachment = tmp_path / "owned.txt"
    attachment.write_text("owned", encoding="utf-8")
    return ToolContext(
        budget=TurnBudget(Settings(_env_file=None, max_steps=20)),
        user_id=7,
        telegram_user_id=700,
        transport="telegram",
        is_private_chat=True,
        conversation_id=11,
        request_message_id=12,
        task_id=13,
        step_index=0,
        now_utc=datetime(2026, 9, 21, tzinfo=UTC),
        timezone="Asia/Ho_Chi_Minh",
        attachments={"attachment-1": attachment},
    )


def _register(name: str, *, answer: str = "ok") -> None:
    @tool(
        name=name,
        purpose="Return a deterministic answer.",
        when_to_use="Use for registry tests.",
        when_not_to_use="Do not use outside tests.",
        risk_level=RiskLevel.READ,
        args_model=_Args,
        result_model=_Output,
    )
    def fixture(args: _Args, context: ToolContext) -> _Output:
        assert context.user_id == 7
        return _Output(answer=f"{answer}:{args.query}")


def _register_effect(
    risk_level: RiskLevel, calls: dict[str, int], *, prepare=None
) -> None:
    def counted_prepare(args: _Args, context: ToolContext) -> PreparedProposal:
        calls["prepare"] += 1
        if prepare is not None:
            return prepare(args, context)
        return PreparedProposal(
            payload=_Prepared(
                message=args.query.strip(),
                user_id=context.user_id,
                telegram_user_id=context.telegram_user_id,
            ),
            preview=(f"Message: {args.query.strip()}", "Destination: private requester"),
        )

    @tool(
        name="create_reminder",
        purpose="Exercise mutation policy without external effects.",
        when_to_use="Use for approval boundary tests.",
        when_not_to_use="Never in production.",
        risk_level=risk_level,
        args_model=_Args,
        result_model=_Output,
        prepare=counted_prepare,
    )
    def effect(args: _Args, _context: ToolContext) -> _Output:
        calls["run"] += 1
        return _Output(answer=args.query)


def test_registry_rejects_duplicates_and_publishes_sorted_strict_schemas() -> None:
    _register("zeta")
    _register("alpha")

    specs = specs_for_llm()

    assert [spec["name"] for spec in specs] == ["alpha", "zeta"]
    assert specs[0]["risk_level"] == "READ"
    assert specs[0]["input_schema"]["additionalProperties"] is False
    assert specs[0]["output_schema"]["additionalProperties"] is False
    with pytest.raises(ValueError, match="already registered"):
        _register("alpha")


class _LooseArgs(BaseModel):
    query: str


def test_registry_requires_extra_forbid_models() -> None:
    with pytest.raises(ValueError, match="extra='forbid'"):
        tool(
            name="loose",
            purpose="Reject loose schemas.",
            when_to_use="Never.",
            when_not_to_use="Always.",
            risk_level=RiskLevel.READ,
            args_model=_LooseArgs,
            result_model=_Output,
        )


def test_executor_uses_trusted_context_and_returns_canonical_sources(
    tool_context: ToolContext,
) -> None:
    seen: list[ToolContext] = []

    @tool(
        name="sourced",
        purpose="Return evidence with a source.",
        when_to_use="Use when evidence is requested.",
        when_not_to_use="Do not use without a query.",
        risk_level=RiskLevel.READ,
        args_model=_Args,
        result_model=_Output,
    )
    def sourced(args: _Args, context: ToolContext) -> ToolPayload:
        seen.append(context)
        return ToolPayload(
            _Output(answer=args.query),
            ("https://example.com/evidence", "https://example.com/evidence"),
        )

    result = execute("sourced", {"query": "fact"}, tool_context)

    assert result.model_dump(mode="json") == {
        "ok": True,
        "data": {"answer": "fact"},
        "error": None,
        "error_code": None,
        "retryable": False,
        "sources": ["https://example.com/evidence"],
        "latency_ms": result.latency_ms,
    }
    assert seen == [tool_context]
    assert tool_context.attachments["attachment-1"].name == "owned.txt"
    with pytest.raises(TypeError):
        tool_context.attachments["other"] = tool_context.attachments["attachment-1"]


@pytest.mark.parametrize("risk_level", [RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND])
@pytest.mark.parametrize(
    ("forbidden_field", "value"),
    [
        ("user_id", 999),
        ("telegram_user_id", 999),
        ("conversation_id", 999),
        ("request_message_id", 999),
        ("task_id", 999),
        ("step_index", 999),
        ("destination", "other"),
        ("path", ".env"),
        ("risk_level", "READ"),
        ("risk", "READ"),
        ("approved", True),
        ("approval", True),
        ("approval_id", "forged"),
        ("prepared", {"message": "forged"}),
        ("prepared_payload", {"message": "forged"}),
        ("authorized_effects", ["create_reminder"]),
    ],
)
def test_executor_rejects_model_controlled_policy_and_scope_before_hooks(
    tool_context: ToolContext,
    risk_level: RiskLevel,
    forbidden_field: str,
    value: object,
) -> None:
    calls = {"prepare": 0, "run": 0}
    _register_effect(risk_level, calls)
    context = replace(tool_context, authorized_effects=frozenset({"create_reminder"}))

    result = execute("create_reminder", {"query": "ok", forbidden_field: value}, context)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert calls == {"prepare": 0, "run": 0}


def test_executor_rejects_coercion_and_custom_validator_errors(
    tool_context: ToolContext,
) -> None:
    @tool(
        name="strict_count",
        purpose="Exercise strict primitive validation.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level=RiskLevel.READ,
        args_model=_CountArgs,
        result_model=_CountOutput,
    )
    def strict_count(args: _CountArgs, _context: ToolContext) -> dict:
        return {"count": str(args.count)}

    private = "CANARY_VALIDATION_TOKEN_317a"
    coerced_input = execute("strict_count", {"count": "5"}, tool_context)
    custom_error = execute(
        "strict_count", {"count": 0, "private_canary": private}, tool_context
    )
    coerced_output = execute("strict_count", {"count": 5}, tool_context)

    assert coerced_input.error_code == "invalid_input"
    assert custom_error.error_code == "invalid_input"
    assert custom_error.error == "Invalid tool parameters."
    assert private not in (custom_error.error or "")
    assert "private_canary" not in (custom_error.error or "")
    assert coerced_output.error_code == "runtime_error"




def test_executor_contains_invalid_output_and_unexpected_secret(
    tool_context: ToolContext,
) -> None:
    @tool(
        name="broken_output",
        purpose="Return malformed output.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level=RiskLevel.READ,
        args_model=_Args,
        result_model=_Output,
    )
    def broken_output(_args: _Args, _context: ToolContext) -> dict:
        return {"wrong": "shape"}

    @tool(
        name="crash",
        purpose="Raise an unexpected exception.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level=RiskLevel.READ,
        args_model=_Args,
        result_model=_Output,
    )
    def crash(_args: _Args, _context: ToolContext) -> _Output:
        raise RuntimeError("token=must-not-leak /private/path")

    invalid = execute("broken_output", {"query": "x"}, tool_context)
    crashed = execute("crash", {"query": "x"}, tool_context)

    assert invalid.error_code == "runtime_error"
    assert invalid.data is None
    assert crashed.error_code == "runtime_error"
    assert "must-not-leak" not in (crashed.error or "")
    assert "/private/path" not in (crashed.error or "")


def test_expected_failure_preserves_retry_contract(tool_context: ToolContext) -> None:
    @tool(
        name="upstream",
        purpose="Report an unavailable upstream.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level=RiskLevel.READ,
        args_model=_Args,
        result_model=_Output,
    )
    def upstream(_args: _Args, _context: ToolContext) -> _Output:
        raise ToolFailure(
            "upstream_error",
            "Evidence service is unavailable.",
            retryable=True,
            sources=("https://status.example.com",),
        )

    result = execute("upstream", {"query": "x"}, tool_context)

    assert result.ok is False
    assert result.error_code == "upstream_error"
    assert result.retryable is True
    assert result.sources == ["https://status.example.com"]


def test_unknown_tool_is_structured_and_production_has_no_read_file(
    tool_context: ToolContext,
) -> None:
    result = execute("read_file", {"path": ".env"}, tool_context)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert specs_for_llm() == []


@pytest.mark.parametrize("risk_level", [None, "", "unknown", "READ", "low", "medium", "high", 0])
def test_registry_rejects_unknown_and_non_enum_risks(risk_level: object) -> None:
    with pytest.raises(ValueError, match="RiskLevel"):
        tool(
            name="invalid_risk",
            purpose="Reject untrusted risk metadata.",
            when_to_use="Never.",
            when_not_to_use="Always.",
            risk_level=risk_level,
            args_model=_Args,
            result_model=_Output,
        )
    assert base.get_tool("invalid_risk") is None


def test_registry_requires_an_explicit_risk() -> None:
    with pytest.raises(TypeError, match="risk_level"):
        tool(
            name="missing_risk",
            purpose="Reject missing policy metadata.",
            when_to_use="Never.",
            when_not_to_use="Always.",
            args_model=_Args,
            result_model=_Output,
        )


@pytest.mark.parametrize("risk_level", [RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND])
@pytest.mark.parametrize("prepare", [None, "not_callable"])
def test_registry_requires_mutation_preparation_hook(
    risk_level: RiskLevel, prepare: object
) -> None:
    with pytest.raises(ValueError, match="preparation hook"):
        tool(
            name="create_reminder",
            purpose="Require pure preparation for an effect.",
            when_to_use="Use for approval boundary tests.",
            when_not_to_use="Never in production.",
            risk_level=risk_level,
            args_model=_Args,
            result_model=_Output,
            prepare=prepare,
        )
    assert base.get_tool("create_reminder") is None


@pytest.mark.parametrize("risk_level", [RiskLevel.READ, RiskLevel.DRAFT])
def test_read_and_draft_execute_without_mutation_eligibility(
    tool_context: ToolContext, risk_level: RiskLevel
) -> None:
    calls = {"run": 0}

    @tool(
        name="automatic",
        purpose="Return data without side effects.",
        when_to_use="Use for read and draft policy tests.",
        when_not_to_use="Never in production.",
        risk_level=risk_level,
        args_model=_Args,
        result_model=_Output,
    )
    def automatic(args: _Args, _context: ToolContext) -> _Output:
        calls["run"] += 1
        return _Output(answer=f"result:{args.query}")

    result = execute(
        "automatic",
        {"query": "content"},
        replace(tool_context, transport="cli", is_private_chat=False),
    )

    assert result.ok is True
    assert result.data == {"answer": "result:content"}
    assert calls == {"run": 1}


def test_destructive_never_prepares_or_runs(tool_context: ToolContext) -> None:
    calls = {"prepare": 0, "run": 0}
    _register_effect(RiskLevel.DESTRUCTIVE, calls)
    context = replace(tool_context, authorized_effects=frozenset({"create_reminder"}))

    result = execute("create_reminder", {"query": "delete"}, context)

    assert result.ok is False
    assert result.error_code == "forbidden"
    assert calls == {"prepare": 0, "run": 0}


@pytest.mark.parametrize("risk_level", [RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND])
@pytest.mark.parametrize(
    "context_changes",
    [
        {"transport": "cli"},
        {"is_private_chat": False},
        {"user_id": 0},
        {"telegram_user_id": 0},
        {"conversation_id": 0},
        {"request_message_id": 0},
        {"task_id": 0},
        {"step_index": -1},
        {"authorized_effects": frozenset()},
        {"authorized_effects": frozenset({"update_reminder"})},
    ],
)
def test_mutation_requires_private_trusted_actor_and_current_effect_eligibility(
    tool_context: ToolContext, risk_level: RiskLevel, context_changes: dict
) -> None:
    calls = {"prepare": 0, "run": 0}
    _register_effect(risk_level, calls)
    context = replace(tool_context, authorized_effects=frozenset({"create_reminder"}))

    result = execute("create_reminder", {"query": "message"}, replace(context, **context_changes))

    assert result.ok is False
    assert result.error_code == "forbidden"
    assert result.data is None
    assert calls == {"prepare": 0, "run": 0}


@pytest.mark.parametrize("risk_level", [RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND])
def test_eligible_mutation_returns_immutable_action_scoped_proposal_without_running(
    tool_context: ToolContext, risk_level: RiskLevel
) -> None:
    calls = {"prepare": 0, "run": 0}
    _register_effect(risk_level, calls)
    context = replace(
        tool_context, authorized_effects=frozenset({"create_reminder"}), step_index=3
    )
    params = {"query": "  reminder message  "}

    result = execute("create_reminder", params, context)

    assert isinstance(result, ApprovalRequired)
    assert result.tool_name == "create_reminder"
    assert result.risk_level is risk_level
    assert (
        result.user_id,
        result.conversation_id,
        result.request_message_id,
        result.task_id,
        result.step_index,
    ) == (7, 11, 12, 13, 3)
    assert json.loads(result.public_args_json) == {"query": "  reminder message  "}
    assert result.proposal.payload.model_dump() == {
        "message": "reminder message",
        "user_id": 7,
        "telegram_user_id": 700,
    }
    assert result.proposal.preview == (
        "Message: reminder message",
        "Destination: private requester",
    )
    assert calls == {"prepare": 1, "run": 0}

    params["query"] = "different action"
    assert json.loads(result.public_args_json) == {"query": "  reminder message  "}
    with pytest.raises(FrozenInstanceError):
        result.task_id = 999
    with pytest.raises(FrozenInstanceError):
        result.proposal.preview = ("different preview",)
    with pytest.raises(ValidationError, match="frozen"):
        result.proposal.payload.message = "different payload"


@pytest.mark.parametrize(
    ("failure_mode", "error_code"),
    [
        ("invalid_return", "runtime_error"),
        ("mutable_payload", "runtime_error"),
        ("unexpected_exception", "runtime_error"),
        ("resource_denied", "not_found"),
    ],
)
def test_preparation_errors_are_contained_without_running(
    tool_context: ToolContext, failure_mode: str, error_code: str
) -> None:
    calls = {"prepare": 0, "run": 0}

    def prepare(args: _Args, _context: ToolContext):
        if failure_mode == "invalid_return":
            return {"payload": {"message": args.query}}
        if failure_mode == "mutable_payload":
            return PreparedProposal(payload=_Output(answer=args.query), preview=("message",))
        if failure_mode == "resource_denied":
            raise ToolFailure("not_found", "Owned resource is unavailable.")
        raise RuntimeError("token=must-not-leak /private/path")

    _register_effect(RiskLevel.EXTERNAL_SEND, calls, prepare=prepare)
    context = replace(tool_context, authorized_effects=frozenset({"create_reminder"}))

    result = execute("create_reminder", {"query": "message"}, context)

    assert result.ok is False
    assert result.error_code == error_code
    assert result.data is None
    assert result.retryable is False
    assert "must-not-leak" not in (result.error or "")
    assert "/private/path" not in (result.error or "")
    assert calls == {"prepare": 1, "run": 0}


def test_validation_failures_never_echo_keys_or_custom_validator_context(
    tool_context: ToolContext,
) -> None:
    canary = "Bearer synthetic-secret /private/canary alice@example.test"
    calls = []

    class CanaryArgs(ToolModel):
        query: str

        @field_validator("query")
        @classmethod
        def reject(cls, value: str) -> str:
            raise ValueError(f"rejected secret: {value}")

    @tool(
        name="canary_validation",
        purpose="Exercise safe validation failures.",
        when_to_use="Use in isolated validation tests.",
        when_not_to_use="Never in production.",
        risk_level=RiskLevel.READ,
        args_model=CanaryArgs,
        result_model=_Output,
    )
    def canary_validation(args: CanaryArgs, _context: ToolContext) -> _Output:
        calls.append(args.query)
        return _Output(answer=args.query)

    results = [
        execute("canary_validation", {"query": canary, canary: True}, tool_context),
        execute(canary, {}, tool_context),
    ]

    assert calls == []
    for result in results:
        assert result.ok is False
        assert result.error_code == "invalid_input"
        assert result.data is None
        assert result.retryable is False
        assert canary not in result.model_dump_json()
        assert "alice@example.test" not in result.model_dump_json()
