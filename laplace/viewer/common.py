"""Shared viewer helpers: templates, redaction filter, date parsing, JSON rendering."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from laplace.config import configured_secrets
from laplace.logging import redact_text

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
JSON_LIMIT = 2_000
PAGE_SIZE = 50


def make_safe_text(secrets: tuple[str, ...]):
    """Every DB-derived string passes through the same redaction as the logs."""
    def safe_text(value: Any) -> str:
        return redact_text("" if value is None else str(value), secrets=secrets)
    return safe_text


def pretty_json(text: str | None, limit: int = JSON_LIMIT) -> str:
    if not text:
        return ""
    try:
        rendered = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        rendered = text
    return rendered if len(rendered) <= limit else rendered[:limit] + "\n… (đã cắt)"


def parse_json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    """``YYYY-MM-DD`` (UTC) → naive bound; ``end`` gives the exclusive start of the next day."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from None
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if end:
        moment += timedelta(days=1)
    return moment.replace(tzinfo=None)


def mask_telegram_id(value: int | None) -> str:
    digits = str(value or 0)
    return f"…{digits[-4:]}" if len(digits) > 4 else digits


def build_templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"])
    )
    templates = Jinja2Templates(env=env)
    templates.env.filters["safe_text"] = make_safe_text(configured_secrets())
    templates.env.filters["pretty_json"] = pretty_json
    templates.env.filters["fmt_dt"] = fmt_dt
    templates.env.filters["mask_id"] = mask_telegram_id
    return templates
