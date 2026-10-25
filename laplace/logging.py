"""Allowlisted operational events and final-output logging privacy protection.

Canonical messages, tool evidence and owner previews must not use this projection.
Logging is configured explicitly at startup, never as an import side effect.
"""

from __future__ import annotations

import copy
import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

SAFE_STOP_REASONS = frozenset({
    "final", "final_after_tool_budget", "final_after_context_recovery", "schema_failure",
    "context_overflow", "execution_error", "effect_incomplete", "cancelled", "step_limit",
    "time_limit", "cost_limit", "budget_unavailable", "repeated_error", "awaiting_approval",
    "approval_invalid", "approval_invalidated", "approval_failed", "approval_cancelled",
    "required_context_too_large", "param_error", "permission_required", "service_error",
    "restart_interrupted",
})
SAFE_EVENTS = SAFE_STOP_REASONS | {
    "tool_call", "compaction_backlog", "unknown_event", "verification", "restart_notice",
}
SAFE_STATUSES = frozenset({
    "pending", "running", "awaiting_approval", "approved", "rejected", "expired", "executing",
    "completed", "failed", "cancelled", "invalidated", "interrupted", "sent", "delivery_unknown",
    "missed", "step_limit", "time_limit", "cost_limit", "settled", "unknown", "reserved",
    "verified", "partial", "unverified", "not_applicable",
})
_EXCEPTION_CODES = frozenset({
    "RuntimeError", "ValueError", "TypeError", "KeyError", "LookupError", "AssertionError",
    "OSError", "IOError", "FileNotFoundError", "PermissionError", "TimeoutError",
    "ConnectionError", "CancelledError", "ValidationError", "JSONDecodeError",
    "BudgetStop", "MissingAPIKeyError", "ToolFailure", "AmbiguousDeliveryError",
    "TelegramForbiddenError", "TelegramUnauthorizedError", "TelegramBadRequest",
    "TelegramRetryAfter", "TelegramNetworkError", "TelegramServerError",
    "APIConnectionError", "APITimeoutError", "APIStatusError", "RateLimitError",
    "AuthenticationError", "BadRequestError", "InternalServerError", "NotFoundError",
    "IntegrityError", "OperationalError", "SQLAlchemyError", "TimeoutException",
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError",
    "ReadError", "WriteError", "HTTPStatusError",
})
SAFE_ERROR_CODES = _EXCEPTION_CODES | {
    "invalid_input", "configuration_error", "forbidden", "not_found", "unsupported_content",
    "upstream_error", "timeout", "runtime_error",
}
_SAFE_TOOLS = frozenset({
    "read_document", "web_search", "fetch_page", "create_reminder", "update_reminder",
    "cancel_reminder",
})
_ENUM_FIELDS = {
    "event": SAFE_EVENTS,
    "tool_name": _SAFE_TOOLS,
    "risk": frozenset({"READ", "DRAFT", "WRITE_REVERSIBLE", "EXTERNAL_SEND", "DESTRUCTIVE"}),
    "status": SAFE_STATUSES,
    "error_code": SAFE_ERROR_CODES,
    "stop_reason": SAFE_STOP_REASONS,
    "pricing_mode": frozenset({"known", "unknown", "configured", "free", "paid", "reserved"}),
    "pricing_status": frozenset({"known", "unknown", "reserved", "settled", "unavailable"}),
    "accounting_status": frozenset({"reserved", "settled", "unknown"}),
    "error_class": frozenset({"transient", "param", "permission", "service"}),
    "decision": frozenset({"continue", "correct_once", "stop"}),
}
_ID_FIELDS = frozenset({"task_id", "approval_id", "tool_call_id"})
_COUNTER_FIELDS = frozenset({
    "step_index", "remaining_chars", "sources_omitted", "max_steps", "steps_used",
    "steps_remaining", "processed_steps", "total_steps", "llm_calls", "logical_calls",
    "physical_attempts", "unknown_attempts", "prompt_tokens", "completion_tokens",
    "total_tokens", "attempt_index", "attempt_count", "attempts", "checks", "passed",
})
_METRIC_FIELDS = frozenset({
    "latency_ms", "active_seconds", "seconds_remaining", "cost_usd", "cost_limit_usd",
    "known_cost_usd", "budget_charge_usd", "input_price_per_million", "output_price_per_million",
})
_BOOL_FIELDS = frozenset({"ok", "retryable", "overshoot", "usage_known"})


def safe_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project fixed values and bounded metrics, never arbitrary strings or objects."""
    projected: dict[str, Any] = {}
    for field, choices in _ENUM_FIELDS.items():
        value = payload.get(field)
        if field == "tool_name" and field not in payload:
            value = payload.get("tool")
        if type(value) is str and value in choices:
            projected[field] = value
        elif field == "event" and field in payload:
            projected[field] = "unknown_event"
    for field in _ID_FIELDS:
        value = payload.get(field)
        if type(value) is int and 0 < value <= 2**63 - 1:
            projected[field] = value
    for field in _COUNTER_FIELDS:
        value = payload.get(field)
        if type(value) is int and 0 <= value <= 10**9:
            projected[field] = value
    for field in _METRIC_FIELDS:
        value = payload.get(field)
        if type(value) in {int, float} and 0 <= value <= 10**12 and math.isfinite(value):
            projected[field] = value
    for field in _BOOL_FIELDS:
        value = payload.get(field)
        if type(value) is bool:
            projected[field] = value
    return projected


def safe_error_code(exc: BaseException) -> str:
    """Return a fixed allowlisted code without inspecting exception text or arguments."""
    name = type(exc).__name__
    return name if name in _EXCEPTION_CODES else "runtime_error"


_REDACTED = "[REDACTED]"
_URL = re.compile(r"(?:https?|file)://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_URL = re.compile(
    r"[?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|key|"
    r"secret|password|passwd|authorization|auth|signature|sig|credential|email|phone|username|"
    r"user[_-]?id|telegram[_-]?user[_-]?id|chat[_-]?id|owner[_-]?id|recipient|destination|file|path)=",
    re.IGNORECASE,
)
_PATTERNS = (
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token|token|password|"
        r"secret|authorization)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk-|gsk_|tvly-|AIza)[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\b\d{5,12}:[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<![\w])\+?\d(?:[ ().\-]*\d){9,14}(?![\w])"),
    re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|~/|/)[^\s<>\"',;(){}\[\]]+"),
)
_URL_CONTENT_PATTERNS = _PATTERNS[:-1]


def _configured_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in secrets if isinstance(value, str) and value},
                        key=len, reverse=True))


def redact_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Scrub recognizable sensitive text; not a classifier for arbitrary user data."""
    configured = _configured_secrets(secrets)

    def redact_url(match: re.Match[str]) -> str:
        url = match.group(0)
        decoded = unquote(url)
        authority = decoded.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
        sensitive_value = any(
            pattern.search(decoded) for pattern in _URL_CONTENT_PATTERNS
        )
        if (
            url.casefold().startswith("file://")
            or "@" in authority
            or _SENSITIVE_URL.search(decoded)
            or sensitive_value
            or any(secret in url for secret in configured)
        ):
            return _REDACTED
        return url

    text = _URL.sub(redact_url, text)
    for secret in configured:
        text = text.replace(secret, _REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class SafeFormatter(logging.Formatter):
    """Sanitize the final rendered line without mutating the shared LogRecord.

    Dependencies may log arbitrary request/response bodies at DEBUG. Their freeform
    bodies are omitted, not guessed safe by regex. App messages must remain coded
    operational text; recognizer redaction is only a secondary safeguard.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        *,
        secrets: Iterable[str] = (),
        formatter: logging.Formatter | None = None,
    ) -> None:
        super().__init__(fmt, datefmt, style)
        self._secrets = _configured_secrets(secrets)
        self._formatter = formatter

    def formatException(self, ei) -> str:
        return f"error_code={safe_error_code(ei[1])}"

    def format(self, record: logging.LogRecord) -> str:
        safe_record = copy.copy(record)
        error_code = safe_error_code(record.exc_info[1]) if record.exc_info else None
        # Never invoke traceback rendering (source lines, SQL and chained errors
        # may contain arbitrary private content). Cached exception text is unsafe too.
        safe_record.exc_info = None
        safe_record.exc_text = None
        safe_record.stack_info = None
        if not (record.name == "laplace" or record.name.startswith("laplace.")):
            safe_record.name = "dependency"
            safe_record.msg = "dependency_log"
            safe_record.args = ()
        formatter = self._formatter
        rendered = formatter.format(safe_record) if formatter else super().format(safe_record)
        if error_code is not None:
            rendered += f" error_code={error_code}"
        return redact_text(rendered, secrets=self._secrets)


def setup_logging(*, secrets: Iterable[str] = (), level: int = logging.INFO) -> None:
    """Cover root and existing handlers, including non-propagating dependency logs."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    configured = _configured_secrets(secrets)
    loggers = [root] + [
        logger for logger in list(logging.Logger.manager.loggerDict.values())
        if isinstance(logger, logging.Logger)
    ]
    handlers = {handler for logger in loggers for handler in logger.handlers}
    for handler in handlers:
        previous = handler.formatter
        if isinstance(previous, SafeFormatter):
            combined = _configured_secrets((*previous._secrets, *configured))
            handler.setFormatter(SafeFormatter(secrets=combined, formatter=previous._formatter))
        else:
            previous = previous or logging.Formatter("%(levelname)s %(name)s %(message)s")
            handler.setFormatter(SafeFormatter(secrets=configured, formatter=previous))
