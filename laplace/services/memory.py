"""Điều phối một worker mỗi user và các thao tác xem/xóa bộ nhớ phiên."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from laplace import repo
from laplace.db import session_scope


@dataclass
class TurnLease:
    """Quyền độc quyền process-local cho một lượt của user."""

    user_id: int
    kind: str = "chat"
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _released: bool = False


_lock = threading.Lock()
_active: dict[int, TurnLease] = {}


def try_reserve_turn(user_id: int, *, kind: str = "chat") -> TurnLease | None:
    """Acquire không chờ; None nghĩa user đang có worker/forget."""
    with _lock:
        if user_id in _active:
            return None
        lease = TurnLease(user_id=user_id, kind=kind)
        _active[user_id] = lease
        return lease


def release_turn(lease: TurnLease) -> None:
    """Chỉ owner lease hiện tại mới được giải phóng reservation."""
    with _lock:
        if lease._released:
            return
        if _active.get(lease.user_id) is not lease:
            raise RuntimeError("TurnLease không còn là owner hiện tại")
        lease._released = True
        del _active[lease.user_id]


def request_cancel(user_id: int) -> bool:
    """Đặt cờ cooperative cancellation cho chat worker, không ngắt thread."""
    with _lock:
        lease = _active.get(user_id)
        if lease is None or lease.kind != "chat":
            return False
        lease.cancel_event.set()
        return True


def user_busy(user_id: int) -> bool:
    """Cho biết user có reservation đang sống."""
    with _lock:
        return user_id in _active


def load_memory(telegram_user_id: int) -> dict | None:
    """Trả plain snapshot committed; user chưa tồn tại không tạo row."""
    with session_scope() as session:
        user = repo.find_user(session, telegram_user_id)
        if user is None:
            return None
        snapshot = repo.session_stats(session, user)
    snapshot["busy"] = user_busy(telegram_user_id)
    return snapshot


def forget_memory(telegram_user_id: int) -> tuple[str, dict | None]:
    """Xóa phiên khi acquire được guard; commit xong mới trả success."""
    lease = try_reserve_turn(telegram_user_id, kind="forget")
    if lease is None:
        return "busy", None
    try:
        with session_scope() as session:
            user = repo.find_user(session, telegram_user_id)
            if user is None:
                return "empty", None
            before = repo.user_usage(session, user)
            result = repo.wipe_session(session, user)
            after = repo.user_usage(session, user)
            if before != after:
                raise RuntimeError("Usage thay đổi trong khi xóa memory")
            result["usage_llm_calls"] = after["llm_calls"]
            return "deleted", result
    finally:
        release_turn(lease)
