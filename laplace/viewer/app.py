"""FastAPI application factory for the localhost observation pages (read-only)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from laplace.db import bind_engine, get_engine, session_scope
from laplace.viewer import queries
from laplace.viewer.common import build_templates, parse_date


def create_app(database_url: str | None = None) -> FastAPI:
    """Bind the process to ``database_url`` (default: configured) and build the app.

    The viewer never migrates or mutates: ``init_db`` would interrupt reserved LLM
    accounting rows of a bot running on the same file, so only the engine is opened.
    """
    if database_url:
        prefix = "sqlite:///"
        if database_url.startswith(prefix) and not Path(database_url[len(prefix):]).is_file():
            raise FileNotFoundError(f"database file not found: {database_url[len(prefix):]}")
        bind_engine(database_url)
    get_engine()
    app = FastAPI(
        title="Laplace's Demon — quan sát", docs_url=None, redoc_url=None, openapi_url=None
    )
    templates = build_templates()

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/tasks", status_code=307)

    @app.get("/tasks")
    def tasks(
        request: Request, status: str | None = None,
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"), page: int = Query(1, ge=1),
    ):
        start, end = parse_date(date_from), parse_date(date_to, end=True)
        with session_scope() as session:
            data = queries.list_tasks(
                session, status=status or None, date_from=start, date_to=end, page=page
            )
        return templates.TemplateResponse(request, "tasks.html", {
            **data, "status": status or "", "date_from": date_from or "", "date_to": date_to or "",
        })

    @app.get("/tasks/{task_id}")
    def task(request: Request, task_id: int):
        with session_scope() as session:
            data = queries.task_detail(session, task_id)
            if data is None:
                raise HTTPException(status_code=404, detail="task not found")
            return templates.TemplateResponse(request, "task_detail.html", data)

    from laplace.viewer.stats_routes import register as register_stats

    register_stats(app, templates)
    return app
