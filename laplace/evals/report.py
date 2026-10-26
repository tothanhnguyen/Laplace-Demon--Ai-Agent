"""Persist a suite result as results.json, report.md and eval_runs/eval_results rows."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from laplace.config import configured_secrets
from laplace.db import session_scope
from laplace.evals.cases import GROUPS
from laplace.evals.runner import CaseResult, SuiteResult
from laplace.logging import redact_text
from laplace.models import EvalResult, EvalRun

REPLY_EXCERPT_CHARS = 500


def _case_dict(result: CaseResult, secrets: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": result.case.id, "group": result.case.group,
        "description": result.case.description, "passed": result.passed,
        "failure_group": result.failure_group, "task_id": result.task_id,
        "stop_reason": result.stop_reason, "duration_ms": result.duration_ms,
        "cost_usd": result.cost_usd, "llm_calls": result.usage.get("llm_calls"),
        "checks": [c.to_dict() for c in result.checks],
        "reply_excerpt": redact_text(result.reply[:REPLY_EXCERPT_CHARS], secrets=secrets),
    }


def suite_dict(suite: SuiteResult) -> dict[str, Any]:
    secrets = configured_secrets()
    return {
        "run_id": suite.run_id, "provider_mode": suite.provider_mode, "model": suite.model,
        "started_at": suite.started_at.isoformat(),
        "finished_at": suite.finished_at.isoformat() if suite.finished_at else None,
        "total": suite.total, "passed": suite.passed,
        "cases": [_case_dict(r, secrets) for r in suite.results],
    }


def group_table(suite: SuiteResult) -> list[tuple[str, int, int]]:
    rows = []
    for group in GROUPS:
        members = [r for r in suite.results if r.case.group == group]
        if members:
            rows.append((group, len(members), sum(1 for r in members if r.passed)))
    return rows


def render_markdown(suite: SuiteResult) -> str:
    data = suite_dict(suite)
    lines = [
        f"# Eval run `{suite.run_id}`", "",
        f"- Mode: `{suite.provider_mode}` · model `{suite.model}`",
        f"- Started {data['started_at']} · finished {data['finished_at']}",
        f"- **{suite.passed}/{suite.total} passed**", "",
        "## Theo nhóm", "", "| Nhóm | Tổng | Đạt | Tỷ lệ |", "|---|---|---|---|",
    ]
    for group, total, passed in group_table(suite):
        lines.append(f"| {group} | {total} | {passed} | {passed / total:.0%} |")
    lines += ["", "## Theo case", "",
              "| Case | Nhóm | Kết quả | Nhóm lỗi | Task | ms | Check trượt |",
              "|---|---|---|---|---|---|---|"]
    for case in data["cases"]:
        failed = "; ".join(f"{c['kind']}: {c['detail']}" for c in case["checks"] if not c["ok"])
        lines.append(
            f"| {case['id']} | {case['group']} | {'PASS' if case['passed'] else 'FAIL'} | "
            f"{case['failure_group'] or ''} | {case['task_id'] or ''} | {case['duration_ms']} | "
            f"{failed.replace('|', '/')} |"
        )
    groups = Counter(c["failure_group"] for c in data["cases"] if c["failure_group"])
    lines += ["", "## Nhóm lỗi", ""]
    if not groups:
        lines.append("Không có case trượt.")
    for name, count in groups.most_common():
        ids = ", ".join(c["id"] for c in data["cases"] if c["failure_group"] == name)
        lines.append(f"- **{name}** ×{count}: {ids}")
    return "\n".join(lines) + "\n"


def write_files(suite: SuiteResult, run_dir: Path) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    results = run_dir / "results.json"
    report = run_dir / "report.md"
    results.write_text(json.dumps(suite_dict(suite), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    report.write_text(render_markdown(suite), encoding="utf-8")
    return results, report


def store(suite: SuiteResult) -> int:
    """Insert the run and its per-case rows into the bound database; returns run row id."""
    with session_scope() as session:
        run = EvalRun(
            run_id=suite.run_id, provider_mode=suite.provider_mode, model=suite.model,
            started_at=suite.started_at.replace(tzinfo=None),
            finished_at=suite.finished_at.replace(tzinfo=None) if suite.finished_at else None,
            total=suite.total, passed=suite.passed,
        )
        session.add(run)
        session.flush()
        for result in suite.results:
            session.add(EvalResult(
                run_id=run.id, case_id=result.case.id, group=result.case.group,
                passed=result.passed, failure_group=result.failure_group,
                task_id=result.task_id, duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                checks_json=json.dumps([c.to_dict() for c in result.checks], ensure_ascii=False),
            ))
        return run.id
