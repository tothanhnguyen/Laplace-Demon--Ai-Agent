"""Khởi tạo engine, migration SQLite nhỏ và vòng đời session."""

import math
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from laplace.config import Settings
from laplace.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Bật kiểm tra FK cho từng SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _create_engine(url: str) -> Engine:
    """Tạo engine; SQLite memory dùng một connection chung cho test."""
    kwargs = {}
    if url.endswith(":memory:"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def get_engine() -> Engine:
    """Trả engine singleton theo Settings hiện tại."""
    global _engine, _session_factory
    if _engine is None:
        _engine = _create_engine(Settings().database_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _migrate_sprint3(engine: Engine) -> None:
    """Thêm cột nullable Sprint 3 cho DB cũ; chạy lặp lại an toàn."""
    if engine.dialect.name != "sqlite":
        return
    additions = {
        "conversations": {
            "summary": "TEXT",
            "summary_until_message_id": "INTEGER",
        },
        "tasks": {
            "request_message_id": "INTEGER REFERENCES messages(id)",
        },
    }
    with engine.begin() as connection:
        schema = inspect(connection)
        tables = set(schema.get_table_names())
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {item["name"] for item in schema.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def _migrate_sprint5(engine: Engine) -> None:
    """Add consent and attempt linkage without rebuilding legacy SQLite tables."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        existing = {item["name"] for item in inspect(connection).get_columns("tool_calls")}
        if "approval_id" not in existing:
            connection.execute(
                text(
                    "ALTER TABLE tool_calls ADD COLUMN approval_id "
                    "INTEGER REFERENCES tool_approvals(id)"
                )
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_calls_approval_id "
                "ON tool_calls (approval_id)"
            )
        )
        existing = {item["name"] for item in inspect(connection).get_columns("llm_calls")}
        additions = {
            "request_message_id": "INTEGER REFERENCES messages(id)",
            "call_group_id": "VARCHAR(64)",
            "attempt_index": "INTEGER",
            "accounting_status": "VARCHAR(16)",
            "budget_charge_usd": "FLOAT",
            "accounting_detail": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                connection.execute(
                    text(f"ALTER TABLE llm_calls ADD COLUMN {name} {sql_type}")
                )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_calls_group_attempt "
                "ON llm_calls (call_group_id, attempt_index)"
            )
        )


def init_db() -> None:
    """Tạo schema mới và nâng cấp additive DB Sprint 2."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_sprint3(engine)
    _migrate_sprint5(engine)
    from laplace.repo import interrupt_reserved_llm_calls

    with session_scope() as session:
        interrupt_reserved_llm_calls(session)


@contextmanager
def session_scope(*, timeout_seconds: float | None = None) -> Iterator[Session]:
    """Commit/rollback a short transaction, optionally bounding SQLite lock waits."""
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be finite and nonnegative")
    engine = get_engine()
    assert _session_factory is not None
    connection = None
    previous_timeout = None
    session = None
    try:
        if timeout_seconds is not None and engine.dialect.name == "sqlite":
            connection = engine.connect()
            previous_timeout = int(
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
            milliseconds = math.floor(min(timeout_seconds * 1000, previous_timeout))
            connection.exec_driver_sql(f"PRAGMA busy_timeout={milliseconds}")
            # PRAGMA autobegins a SQLAlchemy transaction, not the budget transaction.
            connection.commit()
            session = _session_factory(bind=connection)
        else:
            session = _session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            try:
                if connection is not None and previous_timeout is not None:
                    connection.exec_driver_sql(f"PRAGMA busy_timeout={previous_timeout}")
            except BaseException:
                if connection is not None:
                    connection.invalidate()
                raise
            finally:
                if connection is not None:
                    connection.close()


def reset_engine_for_tests(url: str | None = None) -> None:
    """Đổi engine cho test và dispose engine cũ."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    if url is None:
        _engine = None
        _session_factory = None
        return
    _engine = _create_engine(url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
