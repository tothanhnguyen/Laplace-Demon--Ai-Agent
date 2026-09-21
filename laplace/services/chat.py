"""Một lượt chat Sprint 3: context hữu hạn, task ownership và session memory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from laplace import repo
from laplace.config import Settings
from laplace.context import (
    SUMMARY_MAX_CHARS,
    ContextBuild,
    MessageGroup,
    SummaryDocument,
    bounded_tool_observation,
    build_context,
    extractive_summary,
    fit_summary,
    group_history,
    render_task_card,
    retry_group,
)
from laplace.db import session_scope
from laplace.llm.base import LLMProvider, LLMResult, get_provider
from laplace.models import Conversation
from laplace.prompts import AgentAction, system_message
from laplace.services.memory import TurnLease, release_turn, try_reserve_turn
from laplace.tools.base import execute, load_builtin_tools

MAX_STEPS = 5
MAX_SCHEMA_RETRIES = 2
HISTORY_LIMIT = 10
COMPACTION_PAGE_MESSAGES = 8
SPRINT3_TAIL_MESSAGES = 32
ContextStrategy = Literal["full", "window10", "sprint3"]
ContextObserver = Callable[[dict[str, Any]], None]


@dataclass
class ChatReply:
    """Kết quả một lượt chat và usage đã ghi của lượt đó."""

    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class _TurnRecorder:
    """Gom usage của mọi agent/compaction call trong lượt."""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0

    def add(self, result: LLMResult) -> None:
        self.llm_calls += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.cost_usd += result.cost_usd

    def summary(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


def _plain_messages(rows) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for row in rows:
        role = row["role"] if isinstance(row, dict) else row.role
        content = row["content"] if isinstance(row, dict) else row.content
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": content})
    return messages


def _record_llm_result(
    result: LLMResult,
    *,
    recorder: _TurnRecorder,
    provider: LLMProvider,
    user_id: int,
    task_id: int | None,
    purpose: str,
) -> None:
    recorder.add(result)
    with session_scope() as session:
        repo.record_llm_call(
            session,
            task_id=task_id,
            user_id=user_id,
            purpose=purpose,
            provider=provider.name,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )


def _summary_source_ids(document: SummaryDocument) -> tuple[set[int], set[int]]:
    messages: set[int] = set()
    tools: set[int] = set()
    for entry in document.entries:
        messages.update(entry.source_message_ids)
        tools.update(entry.source_tool_call_ids)
    return messages, tools


def _message_exchanges(rows: list[Any]) -> list[list[Any]]:
    """Group persisted rows without copying their potentially large content."""
    exchanges: list[list[Any]] = []
    current: list[Any] = []
    for row in rows:
        role = row["role"] if isinstance(row, dict) else row.role
        if role == "user" and current:
            exchanges.append(current)
            current = []
        current.append(row)
        if role == "assistant":
            exchanges.append(current)
            current = []
    if current:
        exchanges.append(current)
    return exchanges


def _maybe_compact(
    *,
    provider: LLMProvider,
    recorder: _TurnRecorder,
    user_id: int,
    conversation_id: int,
    request_message_id: int,
    request_text: str,
    max_chars: int,
    context_observer: ContextObserver | None = None,
) -> None:
    """Compact at most two bounded oldest-first pages."""
    required = build_context(
        system_message=system_message(),
        history_groups=[],
        request_message={"role": "user", "content": request_text},
        max_chars=max_chars,
    )
    if required.overflow_reason:
        return

    for _ in range(2):
        with session_scope() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return
            previous = conversation.summary
            watermark = conversation.summary_until_message_id
            required_with_summary = build_context(
                system_message=system_message(),
                history_groups=[],
                request_message={"role": "user", "content": request_text},
                rolling_summary=previous,
                max_chars=None,
            )
            history_chars = repo.conversation_message_char_count(
                session,
                conversation_id,
                after_id=watermark,
                before_id=request_message_id,
            )
            if required_with_summary.char_count + history_chars <= max_chars:
                return

            recent_rows = repo.recent_conversation_messages(
                session,
                conversation_id,
                after_id=watermark,
                before_id=request_message_id,
                limit=SPRINT3_TAIL_MESSAGES,
            )
            recent_exchanges = _message_exchanges(recent_rows)
            keep = min(4, len(recent_exchanges))
            while keep > 0:
                tail = [
                    row
                    for exchange in recent_exchanges[-keep:]
                    for row in exchange
                ]
                projected = build_context(
                    system_message=system_message(),
                    history_groups=group_history(_plain_messages(tail)),
                    request_message={"role": "user", "content": request_text},
                    rolling_summary="x" * SUMMARY_MAX_CHARS,
                    max_chars=None,
                )
                if projected.char_count <= max_chars:
                    break
                keep -= 1
            protected = recent_exchanges[-keep:] if keep else []
            protected_start = (
                protected[0][0].id if protected else request_message_id
            )
            candidate_rows = repo.conversation_message_page(
                session,
                conversation_id,
                after_id=watermark,
                before_id=protected_start,
                limit=COMPACTION_PAGE_MESSAGES,
            )
            while candidate_rows and candidate_rows[-1]["role"] != "assistant":
                candidate_rows.pop()
            if not candidate_rows:
                return
            cutoff = candidate_rows[-1]["id"]
            request_ids = [
                row["id"] for row in candidate_rows if row["role"] == "user"
            ]
            tool_rows = repo.tool_rows_for_request_ids(session, request_ids)
            candidate_messages = [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                }
                for row in candidate_rows
            ]

        fallback = extractive_summary(previous, candidate_messages, tool_rows)
        chosen = fallback
        if provider.name != "mock":
            prompt = (
                "Summarize only decisions, reasons, results, sources and user facts. "
                "Preserve source IDs. Do not add facts. Return JSON only.\n"
                + fallback
            )
            compact_messages = [
                {"role": "system", "content": "You compact conversation memory."},
                {"role": "user", "content": prompt},
            ]
            compact_chars = sum(
                len(message["content"]) for message in compact_messages
            )
            if compact_chars <= max_chars:
                if context_observer is not None:
                    context_observer(
                        {
                            "purpose": "compact",
                            "strategy": "sprint3",
                            "messages": [
                                dict(message) for message in compact_messages
                            ],
                            "char_count": compact_chars,
                            "dropped_group_count": 0,
                        }
                    )
                try:
                    result = provider.complete(
                        compact_messages,
                        json_schema=SummaryDocument.model_json_schema(),
                    )
                except Exception:
                    chosen = fallback
                else:
                    # Paid usage must persist before the watermark can advance.
                    _record_llm_result(
                        result,
                        recorder=recorder,
                        provider=provider,
                        user_id=user_id,
                        task_id=None,
                        purpose="compact",
                    )
                    try:
                        if result.parsed is not None:
                            document = SummaryDocument.model_validate(result.parsed)
                            allowed_messages, allowed_tools = _summary_source_ids(
                                SummaryDocument.model_validate_json(fallback)
                            )
                            proposed_messages, proposed_tools = _summary_source_ids(
                                document
                            )
                            if (
                                proposed_messages <= allowed_messages
                                and proposed_tools <= allowed_tools
                                and all(
                                    entry.source_message_ids
                                    or entry.source_tool_call_ids
                                    for entry in document.entries
                                )
                            ):
                                chosen = fit_summary(document)
                    except (TypeError, ValueError):
                        chosen = fallback

        with session_scope() as session:
            updated = repo.update_conversation_summary(
                session,
                conversation_id=conversation_id,
                old_watermark=watermark,
                summary=chosen,
                new_watermark=cutoff,
            )
        if not updated:
            continue

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return
        remaining_chars = repo.conversation_message_char_count(
            session,
            conversation_id,
            after_id=conversation.summary_until_message_id,
            before_id=request_message_id,
        )
        if remaining_chars:
            repo.record_trace(
                session,
                task_id=None,
                conversation_id=conversation_id,
                step_index=0,
                payload={
                    "event": "compaction_backlog",
                    "remaining_chars": remaining_chars,
                },
            )


def _build_for_call(
    *,
    strategy: ContextStrategy,
    history_groups: list[MessageGroup],
    request: str,
    execution_groups: list[MessageGroup],
    task_card: str | None,
    summary: str | None,
    max_chars: int,
) -> ContextBuild:
    return build_context(
        system_message=system_message(),
        history_groups=history_groups,
        request_message={"role": "user", "content": request},
        execution_groups=execution_groups,
        task_card=task_card if strategy == "sprint3" else None,
        rolling_summary=summary if strategy == "sprint3" else None,
        max_chars=max_chars if strategy == "sprint3" else None,
    )


def _next_action(
    provider: LLMProvider,
    *,
    build_messages: Callable[[list[MessageGroup]], ContextBuild],
    record_result: Callable[[LLMResult, str], None],
    observer: ContextObserver | None,
    strategy: ContextStrategy,
) -> tuple[AgentAction | None, str | None]:
    """Validate action; mỗi retry dựng context mới và vẫn chịu budget."""
    schema = AgentAction.model_json_schema()
    retries: list[MessageGroup] = []
    for attempt in range(MAX_SCHEMA_RETRIES + 1):
        built = build_messages(retries)
        purpose = "agent_action" if attempt == 0 else "schema_retry"
        if built.overflow_reason:
            return None, built.overflow_reason
        snapshot = [dict(message) for message in built.messages]
        if observer is not None:
            observer(
                {
                    "purpose": purpose,
                    "strategy": strategy,
                    "messages": [dict(message) for message in snapshot],
                    "char_count": built.char_count,
                    "dropped_group_count": built.dropped_group_count,
                }
            )
        result = provider.complete(snapshot, json_schema=schema)
        record_result(result, purpose)
        if result.parsed is not None:
            try:
                return AgentAction.model_validate(result.parsed), None
            except ValidationError as exc:
                error = str(exc)
        else:
            error = "output is not a JSON object"
        if attempt == MAX_SCHEMA_RETRIES:
            return None, "schema_failure"
        retries.append(retry_group(result.content, error))
    return None, "schema_failure"


def _task_state(action: AgentAction | None) -> dict[str, Any]:
    update = action.task_update if action else None
    return {
        "acceptance_criteria": update.acceptance_criteria if update else None,
        "remaining_work": update.remaining_work if update else None,
    }


def _persist_terminal(
    *,
    conversation_id: int,
    task_id: int | None,
    step_index: int,
    event: str,
    task_status: str,
    reply_text: str,
    action: AgentAction | None,
) -> str | None:
    """Ghi final message/trace và đóng task trong transaction ngắn."""
    card: str | None = None
    with session_scope() as session:
        if task_id is not None:
            repo.append_step(
                session,
                task_id=task_id,
                step_index=step_index,
                name=event,
                status=task_status,
                detail={
                    "action": action.model_dump() if action else None,
                    "observation": None,
                    "task_state": _task_state(action),
                    "stop_reason": event,
                },
            )
            repo.close_task(session, task_id, task_status)
        repo.record_trace(
            session,
            task_id=task_id,
            conversation_id=conversation_id,
            step_index=step_index,
            payload={"event": event, "answer_preview": reply_text[:200]},
        )
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation đã biến mất trong lượt chat")
        repo.add_message(session, conversation, "assistant", reply_text)
        if task_id is not None:
            card = render_task_card(repo.task_snapshot(session, task_id))
    return card


def _full_observation(
    tool_name: str, ok: bool, payload: str, tool_call_id: int, source: str | None
) -> dict[str, str]:
    return {
        "role": "user",
        "content": "UNTRUSTED_TOOL_DATA_JSON:\n"
        + json.dumps(
            {
                "tool": tool_name,
                "ok": ok,
                "tool_call_id": tool_call_id,
                "source": source,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        + "\nDecide the next action as one JSON object.",
    }


def _handle_message(
    telegram_user_id: int,
    username: str | None,
    text: str,
    progress: Callable[[str], None] | None,
    *,
    lease: TurnLease,
    provider: LLMProvider,
    context_strategy: ContextStrategy = "sprint3",
    context_observer: ContextObserver | None = None,
) -> ChatReply:
    """Shared runtime production/eval; caller sở hữu lease cho tới khi hàm trả."""
    load_builtin_tools()
    settings = Settings()
    recorder = _TurnRecorder()
    notify = progress or (lambda _message: None)

    with session_scope() as session:
        user = repo.get_or_create_user(session, telegram_user_id, username)
        conversation = repo.get_or_create_conversation(session, user)
        request_row = repo.add_message(session, conversation, "user", text)
        user_id = user.id
        conversation_id = conversation.id
        request_message_id = request_row.id


    if context_strategy == "sprint3":
        _maybe_compact(
            provider=provider,
            recorder=recorder,
            user_id=user_id,
            conversation_id=conversation_id,
            request_message_id=request_message_id,
            request_text=text,
            context_observer=context_observer,
            max_chars=settings.context_max_chars,
        )

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation không tồn tại")
        summary = conversation.summary
        watermark = (
            conversation.summary_until_message_id
            if context_strategy == "sprint3"
            else None
        )
        backlog_chars = 0
        if context_strategy == "full":
            rows = repo.conversation_messages(
                session,
                conversation_id,
                before_id=request_message_id,
            )
        else:
            rows = repo.recent_conversation_messages(
                session,
                conversation_id,
                after_id=watermark,
                before_id=request_message_id,
                limit=(
                    HISTORY_LIMIT - 1
                    if context_strategy == "window10"
                    else SPRINT3_TAIL_MESSAGES
                ),
            )
        if context_strategy == "sprint3" and rows:
            backlog_chars = repo.conversation_message_char_count(
                session,
                conversation_id,
                after_id=watermark,
                before_id=rows[0].id,
            )
        recent_request_ids = [row.id for row in rows if row.role == "user"]
        recent_tools = repo.tool_rows_for_request_ids(session, recent_request_ids)
    history_groups = group_history(_plain_messages(rows))
    context_summary = (
        extractive_summary(summary, [], recent_tools) if recent_tools else summary
    )
    if backlog_chars:
        notice = (
            "UNSUMMARIZED_HISTORY_BACKLOG: "
            f"{backlog_chars} ký tự lịch sử giữa watermark và recent tail "
            "chưa được tóm tắt; nội dung đó không có trong context lượt này."
        )
        context_summary = f"{context_summary}\n{notice}" if context_summary else notice

    task_id: int | None = None
    task_card: str | None = None
    execution_groups: list[MessageGroup] = []
    terminal_written = False
    step_index = 0
    try:
        for step_index in range(MAX_STEPS + 1):
            if lease.cancel_event.is_set():
                reply_text = "Lượt xử lý đã được hủy trước bước tiếp theo."
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event="cancelled",
                    task_status="cancelled",
                    reply_text=reply_text,
                    action=None,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())


            def prepare(retries: list[MessageGroup]) -> ContextBuild:
                return _build_for_call(
                    strategy=context_strategy,
                    history_groups=history_groups,
                    request=text,
                    execution_groups=execution_groups + retries,
                    task_card=task_card,
                    summary=context_summary,
                    max_chars=settings.context_max_chars,
                )

            def record(result: LLMResult, purpose: str) -> None:
                _record_llm_result(
                    result,
                    recorder=recorder,
                    provider=provider,
                    user_id=user_id,
                    task_id=task_id,
                    purpose=purpose,
                )

            action, stop_reason = _next_action(
                provider,
                build_messages=prepare,
                record_result=record,
                observer=context_observer,
                strategy=context_strategy,
            )
            if action is None:
                overflow = stop_reason == "required_context_too_large"
                reply_text = (
                    "Yêu cầu và trạng thái bắt buộc vượt ngân sách context; "
                    "hãy rút ngắn yêu cầu hoặc tăng LAPLACE_CONTEXT_MAX_CHARS."
                    if overflow
                    else "Mô hình trả dữ liệu không hợp lệ nhiều lần nên lượt này dừng lại."
                )
                event = "context_overflow" if overflow else "schema_failure"
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event=event,
                    task_status="failed",
                    reply_text=reply_text,
                    action=None,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())

            if lease.cancel_event.is_set():
                reply_text = "Lượt xử lý đã được hủy sau khi bước hiện tại hoàn tất."
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event="cancelled",
                    task_status="cancelled",
                    reply_text=reply_text,
                    action=action,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())

            if action.action == "final":
                reply_text = action.final_answer or ""
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event="final",
                    task_status="completed",
                    reply_text=reply_text,
                    action=action,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())

            if step_index == MAX_STEPS:
                reply_text = (
                    f"Đã chạm giới hạn {MAX_STEPS} bước công cụ mà chưa xong; "
                    "lượt này dừng để tránh chạy vô hạn."
                )
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event="step_limit",
                    task_status="step_limit",
                    reply_text=reply_text,
                    action=action,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())

            if lease.cancel_event.is_set():
                continue
            if task_id is None:
                with session_scope() as session:
                    task = repo.create_task(
                        session,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        request_message_id=request_message_id,
                        goal=text,
                    )
                    task_id = task.id
                    task_card = render_task_card(repo.task_snapshot(session, task_id))
                notify(task_card)

            notify(f"Bước {step_index + 1}: chạy tool {action.tool}...")
            result = execute(action.tool or "", action.params)
            payload = str(result.data) if result.ok else result.error
            source = str(action.params.get("path")) if "path" in action.params else None
            with session_scope() as session:
                tool_call = repo.record_tool_call(
                    session,
                    task_id=task_id,
                    tool_name=action.tool or "",
                    params_json=json.dumps(action.params, ensure_ascii=False),
                    result_json=json.dumps(
                        {"ok": result.ok, "payload": payload}, ensure_ascii=False
                    ),
                    ok=result.ok,
                    error=None if result.ok else result.error,
                    latency_ms=0,
                )
                observation = (
                    bounded_tool_observation(
                        tool_name=action.tool or "",
                        ok=result.ok,
                        payload=payload,
                        tool_call_id=tool_call.id,
                        source=source,
                    )
                    if context_strategy == "sprint3"
                    else _full_observation(
                        action.tool or "", result.ok, payload, tool_call.id, source
                    )
                )
                repo.append_step(
                    session,
                    task_id=task_id,
                    step_index=step_index,
                    name=action.tool or "tool",
                    status="completed" if result.ok else "failed",
                    detail={
                        "action": action.model_dump(),
                        "observation": {
                            "tool_call_id": tool_call.id,
                            "ok": result.ok,
                            "source": source,
                            "preview": payload[:200],
                        },
                        "task_state": _task_state(action),
                        "stop_reason": None,
                    },
                )
                repo.record_trace(
                    session,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    step_index=step_index,
                    payload={
                        "event": "tool_call",
                        "tool_call_id": tool_call.id,
                        "tool": action.tool,
                        "ok": result.ok,
                    },
                )
                task_card = render_task_card(repo.task_snapshot(session, task_id))
            execution_groups.append(
                MessageGroup(
                    (
                        {
                            "role": "assistant",
                            "content": json.dumps(action.model_dump(), ensure_ascii=False),
                        },
                        observation,
                    )
                )
            )
            notify(task_card)
    except Exception:
        if task_id is not None and not terminal_written:
            try:
                _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event="execution_error",
                    task_status="failed",
                    reply_text="Lượt xử lý dừng do lỗi thực thi.",
                    action=None,
                )
            except Exception:
                pass
        raise
    raise RuntimeError("Vòng agent kết thúc mà không có terminal result")


def _run_reserved_message(
    lease: TurnLease,
    telegram_user_id: int,
    username: str | None,
    text: str,
    progress: Callable[[str], None] | None = None,
    *,
    provider: LLMProvider | None = None,
    context_strategy: ContextStrategy = "sprint3",
    context_observer: ContextObserver | None = None,
) -> ChatReply:
    """Chạy shared runtime rồi release đúng lease của worker thật."""
    if lease.user_id != telegram_user_id or lease._released:
        raise RuntimeError("TurnLease không hợp lệ cho user")
    try:
        return _handle_message(
            telegram_user_id,
            username,
            text,
            progress,
            lease=lease,
            provider=provider or get_provider(),
            context_strategy=context_strategy,
            context_observer=context_observer,
        )
    finally:
        release_turn(lease)


def handle_message(
    telegram_user_id: int,
    username: str | None,
    text: str,
    progress: Callable[[str], None] | None = None,
) -> ChatReply:
    """Public API tương thích Sprint 2; từ chối hai lượt đồng thời cùng user."""
    lease = try_reserve_turn(telegram_user_id)
    if lease is None:
        return ChatReply("Đang có một yêu cầu khác của bạn được xử lý. Hãy chờ hoặc dùng /cancel.")
    return _run_reserved_message(lease, telegram_user_id, username, text, progress)
