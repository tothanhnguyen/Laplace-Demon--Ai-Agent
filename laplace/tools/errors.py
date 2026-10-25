"""Error taxonomy for tool failures and the bounded transient retry helper.

One pure mapping decides the class of every canonical ``error_code``; the
runtime strategy (retry, correct once, stop) lives in ``services/error_policy``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from laplace.llm.base import BudgetStop
from laplace.services.budget import TurnBudget


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PARAM = "param"
    PERMISSION = "permission"
    SERVICE = "service"


MAX_TRANSIENT_RETRIES = 2
_BACKOFF_SECONDS = (0.5, 1.0)

# Vietnamese labels shown to the owner; never the raw exception text.
ERROR_LABELS: dict[str, str] = {
    "invalid_input": "tham số không hợp lệ",
    "configuration_error": "thiếu cấu hình dịch vụ",
    "forbidden": "không có quyền thực hiện",
    "not_found": "không tìm thấy dữ liệu",
    "unsupported_content": "nội dung không được hỗ trợ",
    "upstream_error": "dịch vụ bên ngoài lỗi",
    "timeout": "hết thời gian chờ",
    "runtime_error": "lỗi thực thi công cụ",
}
CLASS_LABELS: dict[str, str] = {
    ErrorClass.TRANSIENT: "lỗi tạm thời",
    ErrorClass.PARAM: "lỗi đầu vào",
    ErrorClass.PERMISSION: "thiếu quyền",
    ErrorClass.SERVICE: "lỗi dịch vụ",
}


def error_label(error_code: str | None) -> str:
    return ERROR_LABELS.get(error_code or "", ERROR_LABELS["runtime_error"])


def classify(error_code: str | None, *, retryable: bool = False) -> ErrorClass:
    """Map one failed envelope to exactly one class; unknown codes are service errors."""
    if error_code == "timeout" or (error_code == "upstream_error" and retryable):
        return ErrorClass.TRANSIENT
    if error_code in {"invalid_input", "not_found"}:
        return ErrorClass.PARAM
    if error_code == "forbidden":
        return ErrorClass.PERMISSION
    return ErrorClass.SERVICE


def classify_result(result: Any) -> ErrorClass | None:
    """Class of a canonical ToolResult-like object, or None when it succeeded."""
    if getattr(result, "ok", False):
        return None
    return classify(getattr(result, "error_code", None), retryable=bool(
        getattr(result, "retryable", False)
    ))


def is_transient_exception(exc: BaseException) -> bool:
    """Raw network/timeout exceptions escaping a tool count as transient."""
    return isinstance(exc, TimeoutError | ConnectionError)


def backoff_seconds(attempt: int) -> float:
    """Delay before retry number ``attempt`` (1-based); clamped to the table."""
    index = min(max(attempt, 1), len(_BACKOFF_SECONDS)) - 1
    return _BACKOFF_SECONDS[index]


def sleep(seconds: float) -> None:
    """Injectable backoff sleep; tests replace this module attribute."""
    time.sleep(seconds)


def run_with_transient_retry(
    run: Callable[[], Any],
    *,
    is_transient_failure: Callable[[BaseException], bool],
    budget: TurnBudget,
    max_retries: int = MAX_TRANSIENT_RETRIES,
) -> tuple[Any, int]:
    """Call ``run`` up to ``1 + max_retries`` times on transient failures.

    Returns ``(value, attempts)``. Budget/cancel is checked before every retry and
    the backoff is clamped to the remaining active time; a retry that cannot fit
    is not attempted and the last failure is re-raised for normal handling.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return run(), attempts
        except BudgetStop:
            raise
        except Exception as exc:
            # Callers read the physical attempt count from the final failure.
            exc.attempts = attempts  # type: ignore[attr-defined]
            if attempts > max_retries or not is_transient_failure(exc):
                raise
            delay = backoff_seconds(attempts)
            budget.check()
            if budget.remaining_seconds() <= delay:
                raise
            globals()["sleep"](delay)
            budget.check()
