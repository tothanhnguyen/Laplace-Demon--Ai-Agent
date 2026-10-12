"""Điều phối một worker mỗi user và các thao tác xem/xóa bộ nhớ phiên."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from laplace import repo
from laplace.db import session_scope


@dataclass
class TurnLease:
    """Quyền độc quyền process-local cho một lượt của user."""

    user_id: int
    kind: str = "chat"
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _released: bool = False


_lock = threading.RLock()
_active: dict[int, TurnLease] = {}
_paused: dict[int, str] = {}
_accepting_chats = True


@contextmanager
def account_guard() -> Iterator[None]:
    """Serialize admission/publication; never hold across external I/O."""
    with _lock:
        yield


@dataclass
class AttachmentBundle:
    """A single owner for managed uploads across worker and checkpoint lifetimes."""

    paths: Mapping[str, Path]
    managed_root: Path | None = None
    _owner: str = field(default="worker", init=False, repr=False)
    _cleanup_failed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.paths = MappingProxyType(
            {opaque_id: Path(path).absolute() for opaque_id, path in self.paths.items()}
        )
        if self.managed_root is not None:
            self.managed_root = Path(self.managed_root).absolute()

    def transfer_to_checkpoint(self, token: str) -> None:
        with _lock:
            if self._owner != "worker":
                raise RuntimeError("attachment bundle is not worker-owned")
            self._owner = f"checkpoint:{token}"

    def reclaim_from_checkpoint(self, token: str) -> None:
        with _lock:
            if self._owner != f"checkpoint:{token}":
                raise RuntimeError("attachment bundle has a different checkpoint owner")
            self._owner = "worker"

    def _cleanup(self, owner: str) -> bool:
        with _lock:
            if self._owner == "closed":
                return not self._cleanup_failed
            if self._owner != owner:
                return True
            # Claim cleanup before unlinking; publication can no longer transfer it.
            self._owner = "closed"
        root = self.managed_root
        if root is None:
            return True
        failed = root.is_symlink()
        if not failed:
            for path in self.paths.values():
                if path.parent != root:
                    failed = True
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed = True
        self._cleanup_failed = failed
        return not failed

    def cleanup_worker(self) -> bool:
        return self._cleanup("worker")

    def cleanup_checkpoint(self, token: str) -> bool:
        return self._cleanup(f"checkpoint:{token}")


def accepts_chat_admission() -> bool:
    with _lock:
        return _accepting_chats


def start_chat_admission() -> None:
    global _accepting_chats
    with _lock:
        _accepting_chats = True


def stop_chat_admission() -> None:
    global _accepting_chats
    with _lock:
        _accepting_chats = False
        for lease in _active.values():
            if lease.kind == "chat":
                lease.cancel_event.set()


def paused_token(user_id: int) -> str | None:
    with _lock:
        return _paused.get(user_id)


def mark_paused(user_id: int, token: str) -> None:
    with _lock:
        previous = _paused.get(user_id)
        if previous is not None and previous != token:
            raise RuntimeError("a different checkpoint is already published")
        _paused[user_id] = token


def clear_paused(user_id: int, token: str) -> None:
    with _lock:
        if _paused.get(user_id) == token:
            del _paused[user_id]


def admit_chat_turn(user_id: int) -> TurnLease | None:
    with _lock:
        if not _accepting_chats or user_id in _paused:
            return None
        return try_reserve_turn(user_id)


def cancel_requested(user_id: int) -> bool:
    with _lock:
        lease = _active.get(user_id)
        return bool(lease and lease.kind == "chat" and lease.cancel_event.is_set())


def try_reserve_turn(user_id: int, *, kind: str = "chat") -> TurnLease | None:
    """Acquire không chờ; None nghĩa user đang có worker/forget."""
    with _lock:
        if user_id in _active or (kind == "chat" and not _accepting_chats):
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
            # SQLite committed before revoking process-local file/state ownership.
            from laplace.services.chat import discard_user_checkpoints

            cleanup_complete = discard_user_checkpoints(telegram_user_id)
            result["cleanup_complete"] = cleanup_complete
            return "deleted", result
        except repo.ReminderDeliveryInProgress:
            return "delivery_busy", None
    finally:
        release_turn(lease)
