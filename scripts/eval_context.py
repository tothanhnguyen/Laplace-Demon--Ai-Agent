#!/usr/bin/env python3
"""So sánh full/window10/sprint3 trên cùng shared chat runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
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
from laplace.llm.base import LLMProvider, LLMResult, get_provider  # noqa: E402
from laplace.models import Conversation, Trace, User  # noqa: E402
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
        return LLMResult(parsed=parsed, model="content-probe")


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

    def complete(self, messages, *, json_schema=None):
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
                time.sleep(self.pacing_seconds - elapsed)
        try:
            result = self.delegate.complete(messages, json_schema=json_schema)
        except Exception as exc:  # 503 quá tải: thử lại đúng một lần
            if "UNAVAILABLE" not in str(exc) and "503" not in str(exc):
                raise
            self.transient_retries += 1
            time.sleep(15)
            result = self.delegate.complete(messages, json_schema=json_schema)
        finally:
            self._last_call_at = time.monotonic()
        self.observed_usd += result.cost_usd
        self.models.add(result.model)
        pricing = getattr(self.delegate, "_pricing", None)
        if result.prompt_tokens + result.completion_tokens <= 0:
            self.metrics_error = "provider_usage_unavailable"
        elif not isinstance(pricing, dict) or result.model not in pricing:
            self.metrics_error = f"pricing_unavailable:{result.model or 'unknown'}"
        return result


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

def _latest_terminal_event(user_id: int) -> str | None:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == user_id))
        if user is None:
            return None
        payload = session.scalar(
            select(Trace.payload_json)
            .join(Conversation, Trace.conversation_id == Conversation.id)
            .where(Conversation.user_id == user.id)
            .order_by(Trace.id.desc())
            .limit(1)
        )
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed.get("event") if isinstance(parsed, dict) else None


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
    }
    watermarks: list[int | None] = []
    memory: dict[str, Any] | None = None
    terminal_event: str | None = None
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
            )
            answer = reply.text
            watermarks.append(_latest_watermark(user_id))
    except Exception as exc:  # lưu failure thay vì loại khỏi mẫu
        error = f"{type(exc).__name__}: {exc}"
        answer = ""
    finally:
        try:
            memory = load_memory(user_id)
            terminal_event = _latest_terminal_event(user_id)
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
    live_metrics_available = provider.name != "mock" and not (
        isinstance(provider, LimitedProvider) and provider.metrics_error is not None
    )
    schema_instruction_chars = (
        provider.schema_instruction_chars - schema_chars_before
        if isinstance(provider, LimitedProvider)
        else None
    )
    if error is not None:
        runtime_status = "error"
        runtime_stop_reason = error
    elif terminal_event == "final":
        runtime_status = "completed"
        runtime_stop_reason = "final"
    elif terminal_event in {"schema_failure", "context_overflow", "execution_error"}:
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
        "usage": usage
        if live_metrics_available
        else {key: None for key in usage},
        "usage_provenance": "provider_response+laplace.llm.presets"
        if live_metrics_available
        else None,
        "logical_calls": len(observations),
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


def _markdown(results: list[dict[str, Any]], mode: str) -> str:
    lines = [
        f"# Context evaluation — {mode}",
        "",
        "| Case | Strategy | Success | Calls | Sum chars | Peak chars | Tokens | USD | Error |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            f"| {row['case_id']} | {row['strategy']} | "
            f"{int(row['task_success'])} | {row['logical_calls']} | "
            f"{row['sum_application_chars']} | {row['peak_application_chars']} | "
            f"{row['usage']['total_tokens'] or ''} | "
            f"{row['usage']['cost_usd'] or ''} | {row['error'] or ''} |"
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
    with tempfile.TemporaryDirectory(prefix="laplace-context-eval-") as temp:
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
            "estimated_usd": (
                round(sum(row["usage"]["cost_usd"] for row in rows), 6)
                if rows and all(row["usage"]["cost_usd"] is not None for row in rows)
                else None
            ),
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
        "schema_version": 2,
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
    (output / "results.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _markdown(results, args.mode)
    (output / "report.md").write_text(report, encoding="utf-8")
    print(report, end="")
    print(f"results={output / 'results.json'}")


if __name__ == "__main__":
    main()
