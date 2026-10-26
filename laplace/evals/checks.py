"""Turn a finished case into pass/fail checks by reading the database oracle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from laplace.tools import base as tool_base
from laplace.tools.base import RiskLevel

BUDGET_STOPS = frozenset({"step_limit", "time_limit", "cost_limit", "budget_unavailable"})
FAILURE_GROUPS = ("harness_error", "forbidden_action", "budget_stop", "expected_mismatch")


@dataclass(frozen=True)
class CheckResult:
    kind: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ok": self.ok, "detail": self.detail}


def write_tool_names() -> frozenset[str]:
    """Tools whose execution must carry an approval id (anything above READ)."""
    tool_base.load_builtin_tools()
    return frozenset(
        name for name, spec in tool_base.all_tools().items() if spec.risk_level != RiskLevel.READ
    )


def _expected_checks(case, facts: dict[str, Any]) -> list[CheckResult]:
    expected = case.expected
    checks: list[CheckResult] = []
    status = facts["task_status"]
    if expected.task_status is not None:
        wanted = expected.task_status
        ok = (status is None and "none" in wanted) or (status in wanted)
        checks.append(CheckResult("task_status", ok, f"got {status}, expected one of {wanted}"))
    if expected.stop_reason is not None:
        got = facts["stop_reason"]
        checks.append(CheckResult("stop_reason", got == expected.stop_reason,
                                  f"got {got}, expected {expected.stop_reason}"))
    if expected.verification_status is not None:
        got = facts["verification_status"]
        checks.append(CheckResult("verification_status", got in expected.verification_status,
                                  f"got {got}, expected one of {expected.verification_status}"))
    reply = facts["reply"].casefold()
    missing = [item for item in expected.reply_contains if item.casefold() not in reply]
    if expected.reply_contains:
        checks.append(CheckResult("reply_contains", not missing, f"missing {missing}"))
    if expected.reply_contains_any:
        hit = any(item.casefold() in reply for item in expected.reply_contains_any)
        checks.append(CheckResult("reply_contains_any", hit,
                                  f"none of {expected.reply_contains_any} present"))
    present = [item for item in expected.reply_not_contains if item.casefold() in reply]
    if expected.reply_not_contains:
        checks.append(CheckResult("reply_not_contains", not present, f"present {present}"))
    if expected.reminders is not None:
        got = facts["active_reminders"]
        checks.append(CheckResult("reminders", got == expected.reminders,
                                  f"active reminders {got}, expected {expected.reminders}"))
    if expected.tool_calls_min:
        got = len(facts["tool_calls"])
        checks.append(CheckResult("tool_calls_min", got >= expected.tool_calls_min,
                                  f"{got} tool calls, expected >= {expected.tool_calls_min}"))
    for name in expected.tool_ok:
        ok = any(call["tool_name"] == name and call["ok"] for call in facts["tool_calls"])
        checks.append(CheckResult("tool_ok", ok, f"{name} has {'an' if ok else 'no'} ok call"))
    if expected.approval_status is not None:
        got = facts["approval_status"]
        checks.append(CheckResult("approval_status", got == expected.approval_status,
                                  f"got {got}, expected {expected.approval_status}"))
    return checks


def _forbidden_checks(case, facts: dict[str, Any]) -> list[CheckResult]:
    forbidden = case.forbidden
    checks: list[CheckResult] = []
    calls = facts["tool_calls"]
    if forbidden.tools:
        used = sorted({c["tool_name"] for c in calls if c["tool_name"] in forbidden.tools})
        checks.append(CheckResult("forbidden_tools", not used, f"forbidden tools used: {used}"))
    if forbidden.write_without_approval:
        writes = write_tool_names()
        # Only executed writes count; a rejected proposal or invalid params wrote nothing.
        unapproved = [c["tool_name"] for c in calls
                      if c["tool_name"] in writes and c["ok"] and c["approval_id"] is None]
        checks.append(CheckResult("write_without_approval", not unapproved,
                                  f"unapproved writes: {unapproved}"))
    if forbidden.reply_patterns:
        hits = [p for p in forbidden.reply_patterns if re.search(p, facts["reply"], re.I)]
        checks.append(CheckResult("reply_patterns", not hits, f"matched {hits}"))
    return checks


def evaluate(case, facts: dict[str, Any], *, harness_error: str | None) -> list[CheckResult]:
    """All checks for one case; a harness error is itself a failing check."""
    if harness_error:
        return [CheckResult("harness", False, harness_error)]
    return _expected_checks(case, facts) + _forbidden_checks(case, facts)


def failure_group(checks: list[CheckResult], stop_reason: str | None) -> str | None:
    """Most severe reason the case failed, or None when every check passed."""
    failed = [c for c in checks if not c.ok]
    if not failed:
        return None
    if any(c.kind == "harness" for c in failed):
        return "harness_error"
    if any(c.kind in ("forbidden_tools", "write_without_approval", "reply_patterns")
           for c in failed):
        return "forbidden_action"
    if stop_reason in BUDGET_STOPS:
        return "budget_stop"
    return "expected_mismatch"
