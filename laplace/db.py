"""Khởi tạo engine SQLAlchemy và vòng đời session cho tầng lưu trữ."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from laplace.config import Settings
from laplace.models import Base

# Engine singleton, tạo 1 lần dùng lại
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _create_engine(url: str) -> Engine:
    """Tạo engine cho URL đã cho; SQLite in-memory dùng chung một connection.

    StaticPool giữ nguyên connection duy nhất để mọi session cùng thấy schema
    và dữ liệu — thiếu nó mỗi connect sẽ là một DB in-memory rỗng khác nhau.
    """
    # SQLite in-memory cần StaticPool để giữ 1 connection chung
    if url.endswith(":memory:"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url)


def get_engine() -> Engine:
    """Trả về engine singleton theo `Settings().database_url`."""
    global _engine, _session_factory
    # Chưa có engine thì tạo mới theo settings
    if _engine is None:
        _engine = _create_engine(Settings().database_url)
        _session_factory = sessionmaker(bind=_engine)
    return _engine


def init_db() -> None:
    """Tạo toàn bộ bảng chưa tồn tại theo metadata của models."""
    # Tạo tất cả bảng theo models.py
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Mở session, commit khi thành công, rollback khi có exception."""
    get_engine()
    # Mở session → dùng → commit nếu OK, rollback nếu lỗi
    assert _session_factory is not None  # get_engine luôn khởi tạo factory
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
    """Thay engine hiện tại; test truyền URL in-memory, None để về mặc định.

    Engine cũ được dispose để đóng connection; lần `get_engine` kế tiếp sẽ
    khởi tạo lại theo `url` (hoặc theo Settings nếu `url` là None).
    """
    global _engine, _session_factory
    # Đóng engine cũ, tạo engine mới (cho test in-memory)
    if _engine is not None:
        _engine.dispose()
    if url is None:
        _engine = None
        _session_factory = None
        return
    _engine = _create_engine(url)
    _session_factory = sessionmaker(bind=_engine)
