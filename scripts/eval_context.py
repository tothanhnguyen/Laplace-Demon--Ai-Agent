#!/usr/bin/env python3
"""So sánh full/window10/sprint3 trên cùng shared chat runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.context_eval_tasks import CONTEXT_EVAL_CASES, EvalCase  # noqa: E402
from laplace.config import Settings  # noqa: E402
from laplace.db import init_db, reset_engine_for_tests, session_scope  # noqa: E402
from laplace.llm.base import (  # noqa: E402
    BudgetStop,
    CallOptions,
    LLMProvider,
    LLMResult,
    get_provider,
)
from laplace.llm.mock import account_mock_result  # noqa: E402
from laplace.logging import (  # noqa: E402
    SAFE_ERROR_CODES,
    SAFE_STATUSES,
    SAFE_STOP_REASONS,
    redact_text,
    safe_error_code,
)
from laplace.models import Conversation, Task, Trace, User  # noqa: E402
from laplace.services.chat import _run_reserved_message  # noqa: E402
from laplace.services.memory import load_memory, try_reserve_turn  # noqa: E402

FACT_PATTERN = re.compile(r"(?:FACT|REASON|SOURCE)_[A-Z0-9_]+")


class ContentProbe:
    """Provider offline chỉ trả fact nếu fact thật sự có trong messages."""

    name = "mock"

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: CallOptions,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        combined = "\n".join(message["content"] for message in messages)
        latest = messages[-1]["content"] if messages else ""
        normal_user = [
            message["content"]
            for message in messages
            if message["role"] == "user"
            and not message["content"].startswith(
                ("SESSION_STATE_DATA", "UNTRUSTED_TOOL_DATA_JSON", "The previous reply")
            )
        ]
        request = normal_user[-1] if normal_user else ""
        if request.startswith("TOOL:") and not latest.startswith("UNTRUSTED_TOOL_DATA_JSON"):
            path = request.partition(":")[2].strip()
            parsed = {"action": "tool", "tool": "read_file", "params": {"path": path}}
        elif latest.startswith("UNTRUSTED_TOOL_DATA_JSON"):
            facts = list(dict.fromkeys(FACT_PATTERN.findall(latest)))
            source_match = re.search(r'"source"\s*:\s*"([^"\\]+)', latest)
            source = source_match.group(1) if source_match else ""
            answer = " ".join([*facts, source]).strip() or "THIEU_DU_LIEU"
            parsed = {"action": "final", "final_answer": answer}
        elif "?" in request:
            facts = FACT_PATTERN.findall(combined)
            if "hiện tại" in request.lower() and facts:
                families: dict[str, str] = {}
                for fact in facts:
                    family = fact.rsplit("_", 1)[0]
                    families[family] = fact
                facts = list(families.values())
            facts = list(dict.fromkeys(facts))
            associations = list(
                dict.fromkeys(
                    re.findall(
                        r"(Nguồn\s+[AB])\s+ghi\s+(FACT_E03_[AB])",
                        combined,
                        flags=re.IGNORECASE,
                    )
                )
            )
            associated_facts = {fact.casefold() for _, fact in associations}
            parts = [
                *(f"{source} ghi {fact}." for source, fact in associations),
                *(fact for fact in facts if fact.casefold() not in associated_facts),
                *dict.fromkeys(re.findall(r"e0[4-7]\.txt", combined)),
            ]
            answer = " ".join(parts) or "THIEU_DU_LIEU"
            parsed = {"action": "final", "final_answer": answer}
        else:
            parsed = {"action": "final", "final_answer": "Đã ghi nhận."}
        return account_mock_result(
            LLMResult(parsed=parsed, model="content-probe"),
            messages, options=options, json_schema=json_schema,
        )


class LimitedProvider:
    """Giới hạn live run và từ chối usage/giá không có provenance."""

    def __init__(
        self,
        delegate: LLMProvider,
        max_calls: int,
        stop_after_usd: float,
        pacing_seconds: float = 0.0,
    ):
        self.delegate = delegate
        self.name = delegate.name
        self.max_calls = max_calls
        self.stop_after_usd = stop_after_usd
        self.calls = 0
        self.observed_usd = 0.0
        self.models: set[str] = set()
        self.schema_instruction_chars = 0
        self.metrics_error: str | None = None
        self.pacing_seconds = pacing_seconds
        self.transient_retries = 0
        self._last_call_at: float | None = None

    @property
    def stop_reason(self) -> str | None:
        if self.metrics_error is not None:
            return self.metrics_error
        if self.calls >= self.max_calls:
            return "max_calls"
        if self.stop_after_usd > 0 and self.observed_usd >= self.stop_after_usd:
            return "observed_usd"
        return None

    def complete(self, messages, *, options: CallOptions, json_schema=None):
        options.check()
        reason = self.stop_reason
        if reason is not None:
            raise RuntimeError(f"evaluation stopped: {reason}")
        self.calls += 1
        if json_schema is not None:
            prefix = (
                "Respond with a single JSON object that conforms to this "
                "JSON Schema. No prose, no markdown fences, JSON only.\n"
            )
            self.schema_instruction_chars += len(prefix) + len(json.dumps(json_schema))
        if self.pacing_seconds > 0 and self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < self.pacing_seconds:
                self._sleep(self.pacing_seconds - elapsed, options)
        options.check()
        try:
            result = self.delegate.complete(
                messages, options=options, json_schema=json_schema,
            )
        except BudgetStop:
            raise
        except Exception as exc:  # Existing evaluator-only 503 retry, budget checked.
            if "UNAVAILABLE" not in str(exc) and "503" not in str(exc):
                raise
            options.check()
            self.transient_retries += 1
            self._sleep(15, options)
            result = self.delegate.complete(
                messages, options=options, json_schema=json_schema,
            )
        finally:
            self._last_call_at = time.monotonic()
        options.check()
        self.observed_usd += result.cost_usd
        self.models.add(result.model)
        pricing = getattr(self.delegate, "_pricing", None)
        if not result.usage_known:
            self.metrics_error = "provider_usage_unavailable"
        elif not isinstance(pricing, dict) or result.model not in pricing:
            self.metrics_error = "pricing_unavailable"
        return result

    @staticmethod
    def _sleep(seconds: float, options: CallOptions) -> None:
        options.check()
        time.sleep(min(seconds, max(0.0, options.remaining_seconds())))
        options.check()


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _score(case: EvalCase, answer: str) -> dict[str, Any]:
    normalized_answer = _normalize(answer)
    negated = {
        value: bool(
            re.search(
                rf"(?:không(?:\s+phải)?|not)\s+(?:là\s+)?{re.escape(_normalize(value))}",
                normalized_answer,
            )
        )
        for value in case.expected
    }
    expected = {
        value: _normalize(value) in normalized_answer and not negated[value]
        for value in case.expected
    }
    clauses = [
        clause.strip()
        for clause in re.split(r"[.;\n]+", normalized_answer)
        if clause.strip()
    ]
    associations = {
        f"{source} => {fact}": any(
            _normalize(source) in clause and _normalize(fact) in clause
            for clause in clauses
        )
        for source, fact in case.required_associations
    }
    forbidden = {
        value: _normalize(value) in normalized_answer for value in case.forbidden
    }
    return {
        "task_success": (
            all(expected.values())
            and all(associations.values())
            and not any(forbidden.values())
        ),
        "expected": expected,
        "associations": associations,
        "forbidden": forbidden,
        "abstained": _normalize("THIEU_DU_LIEU") in normalized_answer,
    }


def _latest_watermark(user_id: int) -> int | None:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == user_id))
        if user is None:
            return None
        return session.scalar(
            select(Conversation.summary_until_message_id)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.id.desc())
            .limit(1)
        )

def _latest_terminal_state(user_id: int) -> tuple[str | None, str | None]:
    """Read the latest trace and its own task, not an unrelated older task."""
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == user_id))
        if user is None:
            return None, None
        row = session.execute(
            select(Trace.payload_json, Task.status)
            .join(Conversation, Trace.conversation_id == Conversation.id)
            .outerjoin(Task, Trace.task_id == Task.id)
            .where(Conversation.user_id == user.id)
            .order_by(Trace.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None, None
    payload, task_status = row
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None, task_status
    return (parsed.get("event") if isinstance(parsed, dict) else None), task_status


def _run_case(
    case: EvalCase,
    strategy: str,
    provider: LLMProvider,
    run_root: Path,
    user_id: int,
) -> dict[str, Any]:
    case_root = run_root / f"{case.case_id}-{strategy}"
    case_root.mkdir(parents=True)
    for relative, content in case.files.items():
        (case_root / relative).write_text(content, encoding="utf-8")
    database = case_root / "eval.sqlite"
    reset_engine_for_tests(f"sqlite:///{database}")
    init_db()
    observations: list[dict[str, Any]] = []
    usage = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "known_cost_usd": 0.0,
        "budget_charge_usd": 0.0,
        "unknown_attempts": 0,
        "physical_attempts": 0,
    }
    watermarks: list[int | None] = []
    memory: dict[str, Any] | None = None
    terminal_event: str | None = None
    terminal_task_status: str | None = None
    started = time.monotonic()
    schema_chars_before = (
        provider.schema_instruction_chars
        if isinstance(provider, LimitedProvider)
        else 0
    )

    def observe(snapshot: dict[str, Any]) -> None:
        combined = "\n".join(message["content"] for message in snapshot["messages"])
        observations.append(
            {
                "purpose": snapshot["purpose"],
                "char_count": snapshot["char_count"],
                "dropped_group_count": snapshot["dropped_group_count"],
                "evidence_available": {
                    item: item in combined for item in case.expected
                },
            }
        )

    old_cwd = Path.cwd()
    answer = ""
    error: str | None = None
    try:
        os.chdir(case_root)
        for turn in case.turns:
            lease = try_reserve_turn(user_id)
            if lease is None:
                raise RuntimeError("evaluation lease unexpectedly busy")
            reply = _run_reserved_message(
                lease,
                user_id,
                f"eval-{case.case_id}",
                turn,
                provider=provider,
                context_strategy=strategy,
                context_observer=observe,
                transport="cli",
                is_private_chat=True,
            )
            answer = reply.text
            watermarks.append(_latest_watermark(user_id))
    except Exception as exc:  # lưu failure thay vì loại khỏi mẫu
        error = safe_error_code(exc)
        answer = ""
    finally:
        try:
            memory = load_memory(user_id)
            terminal_event, terminal_task_status = _latest_terminal_state(user_id)
            if memory is not None:
                durable_usage = memory["usage"]
                for key in usage:
                    usage[key] = durable_usage[key]
        finally:
            os.chdir(old_cwd)
            reset_engine_for_tests(None)

    scored = _score(case, answer)
    calls_by_purpose: dict[str, int] = {}
    for row in observations:
        purpose = row["purpose"]
        calls_by_purpose[purpose] = calls_by_purpose.get(purpose, 0) + 1
    schema_instruction_chars = (
        provider.schema_instruction_chars - schema_chars_before
        if isinstance(provider, LimitedProvider)
        else None
    )
    if error is not None:
        runtime_status = "error"
        runtime_stop_reason = error
    elif terminal_task_status == "completed" or (
        terminal_task_status is None and terminal_event == "final"
    ):
        runtime_status = "completed"
        runtime_stop_reason = terminal_event or "completed"
    elif terminal_task_status == "failed" or terminal_event in {
        "schema_failure", "context_overflow", "execution_error",
    }:
        runtime_status = "failed"
        runtime_stop_reason = terminal_event
    else:
        runtime_status = terminal_event or "unknown"
        runtime_stop_reason = terminal_event or "unknown"
    return {
        "case_id": case.case_id,
        "rubric_id": case.case_id,
        "strategy": strategy,
        "answer": answer,
        "error": error,
        "runtime_status": runtime_status,
        "stop_reason": runtime_stop_reason,
        **scored,
        "unsupported_claim": any(scored["forbidden"].values()),
        "usage": usage,
        "usage_provenance": (
            "synthetic_mock_attempts"
            if provider.name == "mock"
            else "durable_attempt_ledger+laplace.llm.presets"
        ),
        "logical_calls": usage["llm_calls"],
        "physical_attempts": usage["physical_attempts"],
        "calls_by_purpose": calls_by_purpose,
        "schema_instruction_chars": schema_instruction_chars,
        "sum_application_chars": sum(row["char_count"] for row in observations),
        "peak_application_chars": max(
            (row["char_count"] for row in observations), default=0
        ),
        "tool_executions": memory["tool_calls"] if memory else 0,
        "schema_retries": calls_by_purpose.get("schema_retry", 0),
        "compaction_count": calls_by_purpose.get("compact", 0),
        "truncation_events": sum(
            row["dropped_group_count"] > 0 for row in observations
        ),
        "watermark_progression": watermarks,
        "elapsed_ms": round((time.monotonic() - started) * 1_000),
        "evidence_available_last_call": observations[-1]["evidence_available"]
        if observations
        else {},
        "calls": observations,
    }


_PUBLIC_NUMBERS = frozenset(
    """
    schema_version repeat expected_result_count completed_result_count context_max_chars
    case_count max_calls stop_after_observed_usd pacing_seconds transient_retries
    completed_cases successes unsupported_claims llm_calls prompt_tokens completion_tokens
    total_tokens cost_usd known_cost_usd budget_charge_usd unknown_attempts physical_attempts
    logical_calls schema_instruction_chars sum_application_chars peak_application_chars
    tool_executions schema_retries compaction_count truncation_events elapsed_ms
    char_count dropped_group_count
    """.split()
)
_PUBLIC_BOOLS = frozenset(
    "working_tree_dirty incomplete limit_hit task_success unsupported_claim abstained".split()
)
_PUBLIC_OBJECTS = frozenset("parameters limits usage".split())
_EVIDENCE_MAPS = frozenset(
    "expected associations forbidden evidence_available evidence_available_last_call".split()
)
_STRATEGIES = frozenset(("full", "window10", "sprint3"))
_PURPOSES = frozenset(("agent_action", "agent_final", "schema_retry", "compact"))
_PUBLIC_ENUMS = {
    "mode": frozenset(("offline", "live")),
    "strategy": _STRATEGIES,
    "purpose": _PURPOSES,
    "runtime_status": SAFE_STATUSES | frozenset(("error", "unknown")),
    "usage_provenance": frozenset((
        "synthetic_mock_attempts", "durable_attempt_ledger+laplace.llm.presets",
    )),
}
_PUBLIC_FIELDS = (
    _PUBLIC_NUMBERS | _PUBLIC_BOOLS | _PUBLIC_OBJECTS | _EVIDENCE_MAPS
    | frozenset(_PUBLIC_ENUMS) | frozenset((
        "case_id", "rubric_id", "error", "stop_reason", "requested_provider",
        "resolved_provider", "models", "python", "started_at", "finished_at",
        "code_revision", "source_sha256", "fixture_sha256", "strategies",
        "requested_cases", "aggregate", "results", "calls", "calls_by_purpose",
        "watermark_progression",
    ))
)


def _pseudonym(value: str, kind: str) -> str:
    return f"{kind}-sha256-{hashlib.sha256(value.encode()).hexdigest()}"


def _public_number(value: Any) -> int | float | None:
    if type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value):
        return value
    return None


def _public_identifier(value: Any, kind: str) -> str | None:
    if not isinstance(value, str):
        return None
    public_ids = {case.case_id for case in CONTEXT_EVAL_CASES}
    if kind == "case" and value in public_ids and re.fullmatch(r"E-\d{2}", value):
        return value
    if kind == "model" and value == "content-probe":
        return value
    if kind == "provider" and value in {"mock", "gemini", "openai", "anthropic"}:
        return value
    if re.fullmatch(rf"{kind}-sha256-[a-f0-9]{{64}}", value):
        return value
    return _pseudonym(value, kind)


def _public_field(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _PUBLIC_NUMBERS:
        return _public_number(value)
    if key in _PUBLIC_BOOLS:
        return value if type(value) is bool else None
    if key in _PUBLIC_ENUMS:
        return value if isinstance(value, str) and value in _PUBLIC_ENUMS[key] else "unknown"
    if key in _PUBLIC_OBJECTS:
        return public_projection(value) if isinstance(value, dict) else {}
    if key in _EVIDENCE_MAPS:
        return {
            _public_identifier(reference, "evidence"): present
            for reference, present in value.items()
            if isinstance(reference, str) and type(present) is bool
        } if isinstance(value, dict) else {}
    if key in {"case_id", "rubric_id"}:
        return _public_identifier(value, "case")
    if key in {"requested_provider", "resolved_provider"}:
        return _public_identifier(value, "provider")
    if key == "error":
        return value if isinstance(value, str) and value in SAFE_ERROR_CODES else "unknown_error"
    if key == "stop_reason":
        allowed = SAFE_STOP_REASONS | SAFE_ERROR_CODES | frozenset((
            "max_calls", "observed_usd", "provider_usage_unavailable",
            "pricing_unavailable", "completed", "unknown",
        ))
        return value if isinstance(value, str) and value in allowed else "unknown"
    if key in {"models", "requested_cases"}:
        kind = "model" if key == "models" else "case"
        return [_public_identifier(item, kind) for item in value if isinstance(item, str)] \
            if isinstance(value, list) else []
    if key == "strategies":
        return [item for item in value if isinstance(item, str) and item in _STRATEGIES] \
            if isinstance(value, list) else []
    if key in {"results", "calls"}:
        return [public_projection(item) for item in value if isinstance(item, dict)] \
            if isinstance(value, list) else []
    if key == "aggregate":
        return {
            strategy: public_projection(row)
            for strategy, row in value.items()
            if strategy in _STRATEGIES and isinstance(row, dict)
        } if isinstance(value, dict) else {}
    if key == "calls_by_purpose":
        return {
            purpose: _public_number(count)
            for purpose, count in value.items() if purpose in _PURPOSES
        } if isinstance(value, dict) else {}
    if key == "watermark_progression":
        return [_public_number(item) for item in value] if isinstance(value, list) else []
    if key in {"source_sha256", "fixture_sha256", "code_revision"}:
        return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40,64}", value) else None
    if key == "python":
        return value if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+", value) else None
    if key in {"started_at", "finished_at"}:
        return value if isinstance(value, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00", value
        ) else None
    return None


def _redact_public(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {redact_text(key): _redact_public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_public(item) for item in value]
    return value


def public_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Export metrics only; hash evidence/unknown identities without changing canonical data."""
    return _redact_public({
        key: _public_field(key, value)
        for key, value in payload.items() if key in _PUBLIC_FIELDS
    })


def _write_reports(output: Path, manifest: dict[str, Any]) -> str:
    """Only projected JSON and Markdown belong in the public export directory."""
    projected = public_projection(manifest)
    (output / "results.json").write_text(
        json.dumps(projected, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    report = _markdown(projected["results"], projected["mode"])
    (output / "report.md").write_text(report, encoding="utf-8")
    return report


def _markdown(results: list[dict[str, Any]], mode: str) -> str:
    results = [public_projection(row) for row in results]
    mode = mode if mode in {"offline", "live"} else "unknown"
    lines = [
        f"# Context evaluation — {mode}",
        "",
        "| Case | Strategy | Success | Logical calls | Attempts | Sum chars | Peak chars | "
        "Known tokens | Known USD | Budget charge USD | Unknown attempts | Error |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            f"| {row['case_id']} | {row['strategy']} | "
            f"{int(row['task_success'])} | {row['logical_calls']} | "
            f"{row['usage']['physical_attempts']} | "
            f"{row['sum_application_chars']} | {row['peak_application_chars']} | "
            f"{row['usage']['total_tokens']} | "
            f"{row['usage']['known_cost_usd']} | "
            f"{row['usage']['budget_charge_usd']} | "
            f"{row['usage']['unknown_attempts']} | {row['error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def _code_revision() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _fixture_hash() -> str:
    payload = json.dumps(
        [asdict(case) for case in CONTEXT_EVAL_CASES],
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _source_hash() -> str:
    """Hash the runnable Python sources, including untracked Sprint files."""
    paths = [
        *sorted((ROOT / "laplace").rglob("*.py")),
        ROOT / "scripts" / "eval_context.py",
        ROOT / "experiments" / "context_eval_tasks.py",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _working_tree_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(completed.stdout)


def _run_incomplete(
    results: list[dict[str, Any]],
    expected_result_count: int,
    provider: LLMProvider,
) -> bool:
    return (
        len(results) != expected_result_count
        or any(row.get("runtime_status") != "completed" for row in results)
        or (
            isinstance(provider, LimitedProvider)
            and provider.metrics_error is not None
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--strategies", default="full,window10,sprint3")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=600)
    parser.add_argument("--stop-after-observed-usd", type=float, default=2.0)
    parser.add_argument("--cases", default="")
    parser.add_argument("--pacing-seconds", type=float, default=0.0)
    parser.add_argument(
        "--keep-private-artifacts", action="store_true",
        help="Keep canonical replay data only in ignored var/eval_context, never public exports",
    )
    args = parser.parse_args()
    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    invalid = set(strategies) - {"full", "window10", "sprint3"}
    case_ids = [item.strip() for item in args.cases.split(",") if item.strip()]
    unknown_cases = set(case_ids) - {case.case_id for case in CONTEXT_EVAL_CASES}
    if (
        invalid
        or unknown_cases
        or args.repeat < 1
        or args.max_calls < 1
        or args.pacing_seconds < 0
    ):
        raise SystemExit("Tham số strategy/case/repeat/max-calls/pacing không hợp lệ")
    if args.mode == "offline":
        provider: LLMProvider = ContentProbe()
    else:
        delegate = get_provider(args.provider)
        if delegate.name == "mock":
            raise SystemExit("Live evaluation từ chối provider mock/không xác định")
        provider = LimitedProvider(
            delegate,
            args.max_calls,
            args.stop_after_observed_usd,
            pacing_seconds=args.pacing_seconds,
        )

    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "experiments" / "results" / run_id
    output.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    matrix = [
        (repeat, case_index, case, strategy)
        for repeat in range(args.repeat)
        for case_index, case in enumerate(CONTEXT_EVAL_CASES)
        if not case_ids or case.case_id in case_ids
        for strategy in strategies
    ]
    private_root = ROOT / "var" / "eval_context" / run_id
    if args.keep_private_artifacts:
        private_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    workspace = (
        nullcontext(str(private_root)) if args.keep_private_artifacts
        else tempfile.TemporaryDirectory(prefix="laplace-context-eval-")
    )
    with workspace as temp:
        temp_root = Path(temp)
        for repeat, case_index, case, strategy in matrix:
            row = _run_case(
                case,
                strategy,
                provider,
                temp_root / f"repeat-{repeat}",
                80_000 + repeat * 100 + case_index,
            )
            row["repeat"] = repeat
            results.append(row)
            if isinstance(provider, LimitedProvider) and provider.stop_reason:
                break

    stop_reason = (
        provider.stop_reason if isinstance(provider, LimitedProvider) else None
    )
    expected_result_count = len(matrix)
    incomplete = _run_incomplete(results, expected_result_count, provider)
    aggregate = {}
    for strategy in strategies:
        rows = [row for row in results if row["strategy"] == strategy]
        aggregate[strategy] = {
            "completed_cases": sum(
                row["runtime_status"] == "completed" for row in rows
            ),
            "successes": sum(row["task_success"] for row in rows),
            "unsupported_claims": sum(row["unsupported_claim"] for row in rows),
            "total_tokens": (
                sum(row["usage"]["total_tokens"] for row in rows)
                if rows and all(row["usage"]["total_tokens"] is not None for row in rows)
                else None
            ),
            "known_cost_usd": round(sum(row["usage"]["known_cost_usd"] for row in rows), 6),
            "budget_charge_usd": round(sum(row["usage"]["budget_charge_usd"] for row in rows), 6),
            "unknown_attempts": sum(row["usage"]["unknown_attempts"] for row in rows),
            "physical_attempts": sum(row["usage"]["physical_attempts"] for row in rows),
        }

    limits = {
        "max_calls": args.max_calls if args.mode == "live" else None,
        "stop_after_observed_usd": args.stop_after_observed_usd
        if args.mode == "live"
        else None,
        "pacing_seconds": args.pacing_seconds if args.mode == "live" else None,
        "limit_hit": stop_reason in {"max_calls", "observed_usd"},
        "stop_reason": stop_reason,
    }
    if isinstance(provider, LimitedProvider):
        limits["transient_retries"] = provider.transient_retries
    manifest = {
        "schema_version": 3,
        "mode": args.mode,
        "requested_provider": args.provider if args.mode == "live" else None,
        "resolved_provider": provider.name,
        "models": sorted(provider.models)
        if isinstance(provider, LimitedProvider)
        else ["content-probe"],
        "python": platform.python_version(),
        "strategies": strategies,
        "repeat": args.repeat,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "code_revision": _code_revision(),
        "source_sha256": _source_hash(),
        "working_tree_dirty": _working_tree_dirty(),
        "fixture_sha256": _fixture_hash(),
        "parameters": {
            "context_max_chars": Settings().context_max_chars,
            "case_count": len(CONTEXT_EVAL_CASES),
        },
        "requested_cases": case_ids or [case.case_id for case in CONTEXT_EVAL_CASES],
        "expected_result_count": expected_result_count,
        "completed_result_count": sum(
            row["runtime_status"] == "completed" for row in results
        ),
        "incomplete": incomplete,
        "limits": limits,
        "aggregate": aggregate,
        "results": results,
    }
    if args.keep_private_artifacts:
        private_manifest = private_root / "private-results.json"
        private_manifest.touch(mode=0o600, exist_ok=False)
        private_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for path in private_root.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
    report = _write_reports(output, manifest)
    print(report, end="")
    print("Public metrics: experiments/results/<run-id>/results.json")


if __name__ == "__main__":
    main()
