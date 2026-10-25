"""Turn-level strategy for a failed tool result, after gateway retries.

transient (retries exhausted) and service errors stop the turn; a param error is
handed back to the model exactly once; a permission error stops with a fixed
explanation and no further model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from laplace.tools.errors import ErrorClass, classify_result

MAX_PARAM_CORRECTIONS = 1
DecisionKind = Literal["continue", "correct_once", "stop"]


@dataclass(frozen=True, slots=True)
class Decision:
    kind: DecisionKind
    error_class: ErrorClass | None = None
    stop_reason: str | None = None


def decide(result: Any, *, param_corrections_used: int) -> Decision:
    """Pure mapping from one canonical tool result to the next runtime move."""
    error_class = classify_result(result)
    if error_class is None:
        return Decision("continue")
    if error_class is ErrorClass.PARAM:
        if param_corrections_used < MAX_PARAM_CORRECTIONS:
            return Decision("correct_once", error_class)
        return Decision("stop", error_class, "param_error")
    if error_class is ErrorClass.PERMISSION:
        return Decision("stop", error_class, "permission_required")
    return Decision("stop", error_class, "service_error")


def recovery_facts(corrections_used: int) -> dict[str, Any]:
    """Trusted runtime fact telling the model one correction is allowed, then none."""
    return {
        "class": ErrorClass.PARAM.value,
        "corrections_left": max(0, MAX_PARAM_CORRECTIONS - corrections_used),
    }
