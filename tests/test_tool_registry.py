"""Regression tests for the strict Sprint 4 tool boundary."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, field_validator

from laplace.tools import base
from laplace.tools.base import (
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


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})


@pytest.fixture
def tool_context(tmp_path) -> ToolContext:
    attachment = tmp_path / "owned.txt"
    attachment.write_text("owned", encoding="utf-8")
    return ToolContext(
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
        risk_level="low",
        args_model=_Args,
        result_model=_Output,
    )
    def fixture(args: _Args, context: ToolContext) -> _Output:
        assert context.user_id == 7
        return _Output(answer=f"{answer}:{args.query}")


def test_registry_rejects_duplicates_and_publishes_sorted_strict_schemas() -> None:
    _register("zeta")
    _register("alpha")

    specs = specs_for_llm()

    assert [spec["name"] for spec in specs] == ["alpha", "zeta"]
    assert specs[0]["risk_level"] == "low"
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
            risk_level="low",
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
        risk_level="medium",
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


@pytest.mark.parametrize(
    ("params", "forbidden_field"),
    [
        ({"query": "ok", "user_id": 999}, "user_id"),
        ({"query": "ok", "telegram_user_id": 999}, "telegram_user_id"),
        ({"query": "ok", "destination": "other"}, "destination"),
        ({"query": "ok", "path": ".env"}, "path"),
    ],
)
def test_executor_rejects_model_controlled_identity_and_paths(
    tool_context: ToolContext,
    params: dict,
    forbidden_field: str,
) -> None:
    _register("strict")

    result = execute("strict", params, tool_context)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert forbidden_field in (result.error or "")

def test_executor_rejects_coercion_and_contains_custom_validator_errors(
    tool_context: ToolContext,
) -> None:
    @tool(
        name="strict_count",
        purpose="Exercise strict primitive validation.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level="low",
        args_model=_CountArgs,
        result_model=_CountOutput,
    )
    def strict_count(args: _CountArgs, _context: ToolContext) -> dict:
        return {"count": str(args.count)}

    coerced_input = execute("strict_count", {"count": "5"}, tool_context)
    custom_error = execute("strict_count", {"count": 0}, tool_context)
    coerced_output = execute("strict_count", {"count": 5}, tool_context)

    assert coerced_input.error_code == "invalid_input"
    assert custom_error.error_code == "invalid_input"
    assert "count must be positive" in (custom_error.error or "")
    assert coerced_output.error_code == "runtime_error"




def test_executor_contains_invalid_output_and_unexpected_secret(
    tool_context: ToolContext,
) -> None:
    @tool(
        name="broken_output",
        purpose="Return malformed output.",
        when_to_use="Use for boundary tests.",
        when_not_to_use="Never in production.",
        risk_level="low",
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
        risk_level="low",
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
        risk_level="low",
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
