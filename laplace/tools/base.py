"""Strict tool registry and trusted execution boundary."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RiskLevel = Literal["low", "medium", "high"]
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
    attachments: Mapping[str, Path] = field(default_factory=dict)
    authorized_effects: frozenset[str] = field(default_factory=frozenset)

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

    def llm_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "risk_level": self.risk_level,
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
):
    """Register one deterministic tool contract; duplicate names fail."""

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


def execute(name: str, params: dict[str, Any], context: ToolContext) -> ToolResult:
    """Validate input/output and contain every failure at the tool boundary."""

    started = monotonic()

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
        valid = ", ".join(sorted(_REGISTRY)) or "(none)"
        return failure("invalid_input", f"Unknown tool '{name}'. Available tools: {valid}.")
    try:
        parsed = spec.args_model.model_validate(params)
    except ValidationError as exc:
        details = exc.json(include_input=False, include_url=False)
        return failure("invalid_input", f"Invalid parameters for '{name}': {details}")
    try:
        raw = spec.run(parsed, context)
        payload = raw if isinstance(raw, ToolPayload) else ToolPayload(raw)
        validated = spec.result_model.model_validate(payload.data)
        return ToolResult(
            ok=True,
            data=validated.model_dump(mode="json"),
            sources=_normalize_sources(payload.sources),
            latency_ms=elapsed_ms(),
        )
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
