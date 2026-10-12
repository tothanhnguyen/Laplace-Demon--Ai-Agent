"""Strict tool registry and trusted execution boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from laplace.llm.base import BudgetStop
from laplace.services.budget import TurnBudget


class RiskLevel(StrEnum):
    READ = "READ"
    DRAFT = "DRAFT"
    WRITE_REVERSIBLE = "WRITE_REVERSIBLE"
    EXTERNAL_SEND = "EXTERNAL_SEND"
    DESTRUCTIVE = "DESTRUCTIVE"


ErrorCode = Literal[
    "invalid_input",
    "configuration_error",
    "forbidden",
    "not_found",
    "unsupported_content",
    "upstream_error",
    "timeout",
    "runtime_error",
]

MAX_ERROR_CHARS = 500
MAX_SOURCES = 8
MAX_SOURCE_CHARS = 500
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolModel(BaseModel):
    """Strict base model for every public tool input and output."""

    model_config = ConfigDict(extra="forbid", strict=True)


def canonical_public_args(args: BaseModel) -> str:
    """Stable validated input binding, separate from the prepared effect snapshot."""
    return json.dumps(
        args.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def public_args_hash(args: BaseModel) -> str:
    return hashlib.sha256(canonical_public_args(args).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted per-execution facts that never come from model parameters."""

    user_id: int
    telegram_user_id: int
    transport: Literal["telegram", "cli"]
    is_private_chat: bool
    conversation_id: int
    request_message_id: int
    task_id: int
    step_index: int
    now_utc: datetime
    timezone: str
    budget: TurnBudget
    attachments: Mapping[str, Path] = field(default_factory=dict)
    authorized_effects: frozenset[str] = field(default_factory=frozenset)
    approval_id: int | None = None
    approval_token: str | None = None

    def __post_init__(self) -> None:
        if self.now_utc.tzinfo is None or self.now_utc.utcoffset() is None:
            raise ValueError("now_utc must be timezone-aware")
        if not self.timezone.strip():
            raise ValueError("timezone must not be empty")
        object.__setattr__(
            self,
            "attachments",
            MappingProxyType(
                {
                    str(opaque_id): Path(path).resolve()
                    for opaque_id, path in self.attachments.items()
                }
            ),
        )
        allowed_effects = {
            "create_reminder",
            "update_reminder",
            "cancel_reminder",
        }
        if not self.authorized_effects <= allowed_effects:
            raise ValueError("authorized_effects contains an unknown effect")
        object.__setattr__(self, "authorized_effects", frozenset(self.authorized_effects))


@dataclass(frozen=True, slots=True)
class ToolPayload:
    """Validated tool data plus evidence sources for the canonical envelope."""

    data: Any
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedProposal:
    """Pure preparation: immutable normalized payload and safe preview facts."""

    payload: BaseModel
    preview: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.payload.model_config.get("frozen"):
            raise ValueError("prepared payload must be frozen")
        if not isinstance(self.preview, tuple) or not all(
            isinstance(fact, str) for fact in self.preview
        ):
            raise ValueError("preview must be an immutable tuple of text facts")


@dataclass(frozen=True, slots=True)
class ApprovalRequired:
    """Control outcome, never an execution receipt or reusable permission."""

    tool_name: str
    risk_level: RiskLevel
    proposal: PreparedProposal
    public_args_json: str
    user_id: int
    conversation_id: int
    request_message_id: int
    task_id: int
    step_index: int


class ToolResult(ToolModel):
    """Canonical persisted and model-visible result of one tool execution."""

    ok: bool
    data: Any = None
    error: str | None = Field(default=None, max_length=MAX_ERROR_CHARS)
    error_code: ErrorCode | None = None
    retryable: bool = False
    sources: list[str] = Field(default_factory=list, max_length=MAX_SOURCES)
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent_envelope(self) -> ToolResult:
        if self.ok:
            if self.error is not None or self.error_code is not None or self.retryable:
                raise ValueError("successful tool result cannot contain failure metadata")
        elif self.data is not None or self.error is None or self.error_code is None:
            raise ValueError("failed tool result requires error metadata and no data")
        return self


class ToolFailure(Exception):
    """Expected, safe-to-disclose tool failure."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        sources: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable
        self.sources = sources


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """LLM-visible contract and trusted runtime callable."""

    name: str
    purpose: str
    when_to_use: str
    when_not_to_use: str
    risk_level: RiskLevel
    args_model: type[BaseModel]
    result_model: type[BaseModel]
    run: Callable[[BaseModel, ToolContext], Any] = field(repr=False)
    prepare: Callable[[BaseModel, ToolContext], PreparedProposal] | None = field(
        default=None, repr=False
    )

    def llm_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "risk_level": self.risk_level.value,
            "input_schema": self.args_model.model_json_schema(),
            "output_schema": self.result_model.model_json_schema(),
        }


_REGISTRY: dict[str, ToolSpec] = {}


def _require_strict_model(model: type[BaseModel], role: str) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError(f"{role} must be a Pydantic model")
    if model.model_config.get("extra") != "forbid":
        raise ValueError(f"{role} must set extra='forbid'")


def tool(
    *,
    name: str,
    purpose: str,
    when_to_use: str,
    when_not_to_use: str,
    risk_level: RiskLevel,
    args_model: type[BaseModel],
    result_model: type[BaseModel],
    prepare: Callable[[BaseModel, ToolContext], PreparedProposal] | None = None,
):
    """Register one deterministic tool contract; duplicate names fail."""

    if not isinstance(risk_level, RiskLevel):
        raise ValueError("risk_level must be an explicit RiskLevel")
    if risk_level in {RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND}:
        if not callable(prepare):
            raise ValueError("approval-gated tools require a preparation hook")
    if not _TOOL_NAME.fullmatch(name):
        raise ValueError("tool name must be stable snake_case")
    for label, value in (
        ("purpose", purpose),
        ("when_to_use", when_to_use),
        ("when_not_to_use", when_not_to_use),
    ):
        if not value.strip():
            raise ValueError(f"{label} must not be empty")
    _require_strict_model(args_model, "args_model")
    _require_strict_model(result_model, "result_model")
    if name in _REGISTRY:
        raise ValueError(f"tool '{name}' is already registered")

    def decorator(
        func: Callable[[BaseModel, ToolContext], Any],
    ) -> Callable[[BaseModel, ToolContext], Any]:
        _REGISTRY[name] = ToolSpec(
            name=name,
            purpose=purpose,
            when_to_use=when_to_use,
            when_not_to_use=when_not_to_use,
            risk_level=risk_level,
            args_model=args_model,
            result_model=result_model,
            run=func,
            prepare=prepare,
        )
        return func

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_tools() -> dict[str, ToolSpec]:
    return {name: _REGISTRY[name] for name in sorted(_REGISTRY)}


def specs_for_llm(names: Collection[str] | None = None) -> list[dict[str, Any]]:
    tools = all_tools()
    selected = tools.values() if names is None else (tools[name] for name in names if name in tools)
    return [spec.llm_spec() for spec in selected]


def load_builtin_tools() -> None:
    """Import the explicit production tool modules exactly once."""

    from laplace.tools import documents, reminders, web  # noqa: F401


def _bounded_error(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= MAX_ERROR_CHARS:
        return normalized
    return normalized[: MAX_ERROR_CHARS - 1] + "…"


def _normalize_sources(sources: tuple[str, ...]) -> list[str]:
    if len(sources) > MAX_SOURCES:
        raise ToolFailure("runtime_error", "Tool returned too many sources.")
    normalized: list[str] = []
    for source in sources:
        value = str(source).strip()
        if not value or len(value) > MAX_SOURCE_CHARS:
            raise ToolFailure("runtime_error", "Tool returned an invalid source.")
        if value not in normalized:
            normalized.append(value)
    return normalized


def execute(
    name: str, params: dict[str, Any], context: ToolContext
) -> ToolResult | ApprovalRequired:
    """Validate and apply policy before any callable can cause side effects."""

    started = monotonic()
    if context.approval_id is None:
        context.budget.admit_tool()
    else:
        context.budget.check()

    def elapsed_ms() -> int:
        return max(0, int((monotonic() - started) * 1000))

    def failure(
        error_code: ErrorCode,
        error: str,
        *,
        retryable: bool = False,
        sources: tuple[str, ...] = (),
    ) -> ToolResult:
        try:
            normalized_sources = _normalize_sources(sources)
        except ToolFailure:
            normalized_sources = []
        return ToolResult(
            ok=False,
            error=_bounded_error(error),
            error_code=error_code,
            retryable=retryable,
            sources=normalized_sources,
            latency_ms=elapsed_ms(),
        )

    spec = get_tool(name)
    if spec is None:
        return failure("invalid_input", "Unknown tool.")
    try:
        parsed = spec.args_model.model_validate(params)
    except ValidationError:
        return failure("invalid_input", "Invalid tool parameters.")
    try:
        if spec.risk_level == RiskLevel.DESTRUCTIVE:
            return failure("forbidden", "Destructive tools cannot be executed.")
        requires_approval = spec.risk_level in {
            RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND
        }
        if (context.approval_id is not None or context.approval_token is not None) and (
            not requires_approval
            or context.approval_id is None
            or not context.approval_token
        ):
            return failure("forbidden", "Approval binding does not match this action.")
        if spec.risk_level in {RiskLevel.WRITE_REVERSIBLE, RiskLevel.EXTERNAL_SEND}:
            if (
                context.transport != "telegram"
                or not context.is_private_chat
                or min(
                    context.user_id,
                    context.telegram_user_id,
                    context.conversation_id,
                    context.request_message_id,
                    context.task_id,
                ) <= 0
                or context.step_index < 0
            ):
                return failure("forbidden", "Approval requires a private Telegram request.")
            if name not in context.authorized_effects:
                return failure(
                    "forbidden",
                    "Use /remind, /remind_update, or /remind_cancel "
                    "in the current private Telegram message.",
                )
            if context.approval_id is None:
                if spec.prepare is None:
                    return failure("forbidden", "This tool cannot prepare an approval request.")
                proposal = spec.prepare(parsed, context)
                context.budget.check()
                if not isinstance(proposal, PreparedProposal):
                    return failure("runtime_error", "Tool returned an invalid prepared proposal.")
                return ApprovalRequired(
                    tool_name=name,
                    risk_level=spec.risk_level,
                    proposal=proposal,
                    public_args_json=canonical_public_args(parsed),
                    user_id=context.user_id,
                    conversation_id=context.conversation_id,
                    request_message_id=context.request_message_id,
                    task_id=context.task_id,
                    step_index=context.step_index,
                )
        raw = spec.run(parsed, context)
        context.budget.check()
        if requires_approval:
            if not isinstance(raw, ToolResult) or not raw.ok:
                return failure("runtime_error", "Approved tool did not return a committed receipt.")
            spec.result_model.model_validate(raw.data)
            return raw
        payload = raw if isinstance(raw, ToolPayload) else ToolPayload(raw)
        validated = spec.result_model.model_validate(payload.data)
        return ToolResult(
            ok=True,
            data=validated.model_dump(mode="json"),
            sources=_normalize_sources(payload.sources),
            latency_ms=elapsed_ms(),
        )
    except BudgetStop:
        raise
    except ToolFailure as exc:
        return failure(
            exc.error_code,
            exc.message,
            retryable=exc.retryable,
            sources=exc.sources,
        )
    except ValidationError:
        return failure("runtime_error", "Tool returned data that violates its output schema.")
    except Exception:
        return failure("runtime_error", f"Tool '{name}' failed during execution.")
