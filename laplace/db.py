"""Khởi tạo engine, migration SQLite nhỏ và vòng đời session."""

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


def init_db() -> None:
    """Tạo schema mới và nâng cấp additive DB Sprint 2."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_sprint3(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Mở session, commit thành công hoặc rollback khi lỗi."""
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
