"""Evaluation run pages: list of runs, per-run groups/cases, CSV export."""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from laplace.evals.cases import GROUPS
from laplace.models import EvalResult, EvalRun
from laplace.viewer.common import parse_json


def _has_eval_tables(session: Session) -> bool:
    return inspect(session.get_bind()).has_table("eval_runs")


def list_runs(session: Session) -> list[EvalRun]:
    if not _has_eval_tables(session):
        return []
    return list(session.scalars(select(EvalRun).order_by(EvalRun.id.desc())))


def run_detail(session: Session, run_id: str) -> dict[str, Any] | None:
    if not _has_eval_tables(session):
        return None
    run = session.scalar(select(EvalRun).where(EvalRun.run_id == run_id))
    if run is None:
        return None
    groups = session.execute(
        select(EvalResult.group, func.count(EvalResult.id), func.sum(EvalResult.passed))
        .where(EvalResult.run_id == run.id).group_by(EvalResult.group)
    ).all()
    order = {name: index for index, name in enumerate(GROUPS)}
    group_rows = sorted(
        ((g, int(total), int(passed or 0)) for g, total, passed in groups),
        key=lambda row: order.get(row[0], 99),
    )
    results = session.scalars(
        select(EvalResult).where(EvalResult.run_id == run.id).order_by(EvalResult.case_id)
    ).all()
    failure_groups = session.execute(
        select(EvalResult.failure_group, func.count(EvalResult.id))
        .where(EvalResult.run_id == run.id, EvalResult.failure_group.is_not(None))
        .group_by(EvalResult.failure_group).order_by(func.count(EvalResult.id).desc())
    ).all()
    return {
        "run": run, "groups": group_rows, "failure_groups": failure_groups,
        "results": [{"row": r, "checks": parse_json(r.checks_json) or []} for r in results],
        "total_cost": sum(r.cost_usd for r in results),
    }


def run_csv(results: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["case_id", "group", "passed", "failure_group", "task_id", "duration_ms",
                     "cost_usd", "failed_checks"])
    for item in results:
        row = item["row"]
        failed = "; ".join(f"{c['kind']}: {c['detail']}" for c in item["checks"] if not c["ok"])
        writer.writerow([row.case_id, row.group, int(row.passed), row.failure_group or "",
                         row.task_id or "", row.duration_ms, f"{row.cost_usd:.6f}", failed])
    return buffer.getvalue()
