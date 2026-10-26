"""Routes for /stats and /evals pages plus their CSV exports."""

from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from laplace.db import session_scope
from laplace.viewer import evals_view, stats
from laplace.viewer.common import parse_date


def _csv_response(body: str, filename: str) -> PlainTextResponse:
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return PlainTextResponse(
        body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def register(app: FastAPI, templates) -> None:
    @app.get("/stats")
    def stats_page(
        request: Request, date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
    ):
        start, end = parse_date(date_from), parse_date(date_to, end=True)
        with session_scope() as session:
            data = stats.task_stats(session, date_from=start, date_to=end)
        return templates.TemplateResponse(request, "stats.html", {
            **data, "date_from": date_from or "", "date_to": date_to or "",
        })

    @app.get("/stats.csv")
    def stats_csv(
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
    ):
        start, end = parse_date(date_from), parse_date(date_to, end=True)
        with session_scope() as session:
            data = stats.task_stats(session, date_from=start, date_to=end)
        return _csv_response(stats.stats_csv(data["by_day"]), "stats.csv")

    @app.get("/evals")
    def evals_page(request: Request):
        with session_scope() as session:
            runs = evals_view.list_runs(session)
            return templates.TemplateResponse(request, "evals.html", {"runs": runs})

    @app.get("/evals/{run_id}.csv")
    def eval_run_csv(run_id: str):
        with session_scope() as session:
            data = evals_view.run_detail(session, run_id)
            if data is None:
                raise HTTPException(status_code=404, detail="run not found")
            return _csv_response(evals_view.run_csv(data["results"]), f"{run_id}.csv")

    # Registered after the .csv route so the suffix is not swallowed by {run_id}.
    @app.get("/evals/{run_id}")
    def eval_run_page(request: Request, run_id: str):
        with session_scope() as session:
            data = evals_view.run_detail(session, run_id)
            if data is None:
                raise HTTPException(status_code=404, detail="run not found")
            return templates.TemplateResponse(request, "eval_run.html", data)
