"""Mô hình ORM SQLAlchemy 2.0 cho lưu trữ, memory và reminder."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Trả về thời điểm hiện tại theo UTC để mọi mốc thời gian đồng nhất."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Lớp gốc cho mọi bảng; giữ chung metadata để create_all một lần."""


class User(Base):
    """Người dùng Telegram, định danh duy nhất qua telegram_user_id."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user")
    reminders: Mapped[list["Reminder"]] = relationship(back_populates="user")


class Conversation(Base):
    """Một phiên hội thoại của người dùng, chứa chuỗi tin nhắn theo thời gian."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_until_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    """Tin nhắn trong hội thoại với vai trò user/assistant/system."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Task(Base):
    """Nhiệm vụ Agent do người dùng khởi tạo, theo dõi qua status."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True
    )
    request_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # Sprint 6: evidence verification axis, independent of lifecycle status.
    verification_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Step(Base):
    """Một bước của nhiệm vụ, đánh số theo step_index để tái hiện tiến trình."""

    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    step_index: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ToolApproval(Base):
    """Durable single-generation consent and atomic effect receipt."""

    __tablename__ = "tool_approvals"
    __table_args__ = (
        Index(
            "uq_tool_approvals_active_user",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'approved')"),
        ),
        Index(
            "uq_tool_approvals_active_task",
            "task_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'approved')"),
        ),
        Index("ix_tool_approvals_expiry", "status", "expires_at"),
        Index("ix_tool_approvals_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    public_args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ToolCall(Base):
    """Log một lần gọi tool: tham số, kết quả JSON và độ trễ để soi lỗi."""

    __tablename__ = "tool_calls"
    __table_args__ = (Index("uq_tool_calls_approval_id", "approval_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_approvals.id"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LLMCall(Base):
    """Legacy estimates and durable physical-attempt accounting.

    Nullable attempt fields preserve historical rows; direct ownership survives
    deletion of the request and task that originally incurred the usage.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("uq_llm_calls_group_attempt", "call_group_id", "attempt_index", unique=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    request_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    call_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accounting_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    budget_charge_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    accounting_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Trace(Base):
    """Snapshot JSON theo từng bước để tái hiện quá trình suy luận của Agent."""

    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True
    )
    step_index: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Reminder(Base):
    """Durable owner-scoped one-shot Telegram reminder."""

    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_due", "status", "next_attempt_at_utc", "id"),
        Index("ix_reminders_user_status_due", "user_id", "status", "due_at_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_attempt_at_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_attempt_at_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="reminders")


class EvalRun(Base):
    """One evaluation suite run (Sprint 7); per-case rows live in EvalResult."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)


class EvalResult(Base):
    """Outcome of one case in one run; checks_json holds kind/ok/detail per check."""

    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    checks_json: Mapped[str] = mapped_column(Text, default="[]")
