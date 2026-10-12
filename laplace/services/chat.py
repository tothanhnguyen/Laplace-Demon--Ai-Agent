"""Một lượt chat Sprint 3: context hữu hạn, task ownership và session memory."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from laplace import repo
from laplace.config import Settings
from laplace.context import (
    DOCUMENT_TOOL_OBSERVATION_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TOOL_OBSERVATION_MAX_CHARS,
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
from laplace.tools.base import ToolContext, ToolResult, execute, load_builtin_tools

MAX_STEPS = 5
MAX_SCHEMA_RETRIES = 2
HISTORY_LIMIT = 10
COMPACTION_PAGE_MESSAGES = 8
SPRINT3_TAIL_MESSAGES = 32
ContextStrategy = Literal["full", "window10", "sprint3"]
Transport = Literal["telegram", "cli"]
ContextObserver = Callable[[dict[str, Any]], None]


_REMINDER_DIRECTIVES = {
    "create_reminder": "/remind",
    "update_reminder": "/remind_update",
    "cancel_reminder": "/remind_cancel",
}
REMINDER_DIRECTIVES = frozenset(_REMINDER_DIRECTIVES.values())
_WEB_SEARCH_STARTS = (
    "/web_search",
    "tìm web",
    "tìm kiếm web",
    "tìm trên web",
    "tìm kiếm trên web",
    "tìm trên internet",
    "tra cứu web",
    "tra cứu trên web",
    "hãy tìm web",
    "hãy tìm kiếm web",
    "hãy tra cứu web",
    "search web",
    "search the web",
    "please search the web",
    "web search",
)
_SOURCE_READ_REQUESTS = (
    "đọc ít nhất một nguồn",
    "đọc nguồn",
    "đọc trang nguồn",
    "read at least one source",
    "read a source",
    "read the source",
)


def _has_directive(text: str, directive: str) -> bool:
    """Recognize an explicit command at message or semicolon/newline clause start."""
    clauses = text.casefold().replace("\n", ";").split(";")
    return any(
        clause.strip().split(maxsplit=1)[0] == directive for clause in clauses if clause.strip()
    )


def _required_web_tools(text: str) -> frozenset[str]:
    """Require web retrieval only when the current message explicitly asks for it."""
    normalized = text.casefold()
    clauses = re.split(r"[\n;.!?]+", normalized)
    if not any(
        clause.strip().startswith(_WEB_SEARCH_STARTS)
        for clause in clauses
        if clause.strip()
    ):
        return frozenset()
    required = {"web_search"}
    if any(phrase in normalized for phrase in _SOURCE_READ_REQUESTS):
        required.add("fetch_page")
    return frozenset(required)


def _pending_web_tools(
    required: frozenset[str],
    attempted: set[str],
    search_urls: tuple[str, ...],
) -> tuple[str, ...]:
    """Return explicit web obligations that are executable with available evidence."""
    pending: list[str] = []
    if "web_search" in required and "web_search" not in attempted:
        pending.append("web_search")
    if (
        "fetch_page" in required
        and "fetch_page" not in attempted
        and search_urls
    ):
        pending.append("fetch_page")
    return tuple(pending)


def _required_web_action(
    text: str,
    required: frozenset[str],
    attempted: set[str],
    search_urls: tuple[str, ...],
) -> AgentAction | None:
    pending = _pending_web_tools(required, attempted, search_urls)
    if not pending:
        return None
    query = text.strip()[:500]
    if pending[0] == "web_search":
        return AgentAction(
            action="tool",
            tool="web_search",
            params={"query": query},
        )
    return AgentAction(
        action="tool",
        tool="fetch_page",
        params={"url": search_urls[0], "query": query},
    )


def _authorized_effects(text: str, transport: Transport, is_private_chat: bool) -> frozenset[str]:
    """Mint mutation capabilities only from explicit private Telegram directives."""
    if transport != "telegram" or not is_private_chat:
        return frozenset()
    return frozenset(
        effect
        for effect, directive in _REMINDER_DIRECTIVES.items()
        if _has_directive(text, directive)
    )

def _effect_outcome(result: ToolResult) -> dict[str, Any]:
    """Keep compact trusted lifecycle evidence independent of evictable observations."""
    outcome: dict[str, Any] = {
        "ok": result.ok,
        "error_code": result.error_code,
    }
    if result.ok and isinstance(result.data, dict):
        for key in ("id", "run_at", "status"):
            value = result.data.get(key)
            if isinstance(value, (int, str)):
                outcome[key] = value
    return outcome


def _with_effect_receipts(
    reply_text: str,
    effect_outcomes: Mapping[str, Mapping[str, Any]],
) -> str:
    labels = {
        "create_reminder": "Đã tạo",
        "update_reminder": "Đã cập nhật",
        "cancel_reminder": "Đã hủy",
    }
    lines = []
    for effect in ("create_reminder", "update_reminder", "cancel_reminder"):
        outcome = effect_outcomes.get(effect)
        if not outcome or outcome.get("ok") is not True:
            continue
        reminder_id = outcome.get("id")
        run_at = outcome.get("run_at")
        status = outcome.get("status")
        detail = f"reminder #{reminder_id}"
        if run_at:
            detail += f" lúc {run_at}"
        if status:
            detail += f" ({status})"
        lines.append(f"- {labels[effect]} {detail}.")
    if not lines:
        return reply_text
    receipt = "Thao tác đã xác nhận:\n" + "\n".join(lines)
    return f"{receipt}\n\n{reply_text}" if reply_text else receipt


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
    runtime_facts: Mapping[str, Any] | None = None,
    context_observer: ContextObserver | None = None,
) -> None:
    """Compact at most two bounded oldest-first pages."""
    required = build_context(
        system_message=system_message(runtime_facts),
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
                system_message=system_message(runtime_facts),
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
                tail = [row for exchange in recent_exchanges[-keep:] for row in exchange]
                projected = build_context(
                    system_message=system_message(runtime_facts),
                    history_groups=group_history(_plain_messages(tail)),
                    request_message={"role": "user", "content": request_text},
                    rolling_summary="x" * SUMMARY_MAX_CHARS,
                    max_chars=None,
                )
                if projected.char_count <= max_chars:
                    break
                keep -= 1
            protected = recent_exchanges[-keep:] if keep else []
            protected_start = protected[0][0].id if protected else request_message_id
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
            request_ids = [row["id"] for row in candidate_rows if row["role"] == "user"]
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
                "Preserve source IDs. Do not add facts. Return JSON only.\n" + fallback
            )
            compact_messages = [
                {"role": "system", "content": "You compact conversation memory."},
                {"role": "user", "content": prompt},
            ]
            compact_chars = sum(len(message["content"]) for message in compact_messages)
            if compact_chars <= max_chars:
                if context_observer is not None:
                    context_observer(
                        {
                            "purpose": "compact",
                            "strategy": "sprint3",
                            "messages": [dict(message) for message in compact_messages],
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
                            proposed_messages, proposed_tools = _summary_source_ids(document)
                            if (
                                proposed_messages <= allowed_messages
                                and proposed_tools <= allowed_tools
                                and all(
                                    entry.source_message_ids or entry.source_tool_call_ids
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
    runtime_facts: Mapping[str, Any],
    max_chars: int,
    include_tools: bool = True,
    tool_names: frozenset[str] | None = None,
) -> ContextBuild:
    return build_context(
        system_message=system_message(
            runtime_facts,
            include_tools=include_tools,
            tool_names=tool_names,
        ),
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
    final_only: bool = False,
) -> tuple[AgentAction | None, str | None]:
    """Validate action; mỗi retry dựng context mới và vẫn chịu budget."""
    schema = AgentAction.model_json_schema()
    retries: list[MessageGroup] = []
    for attempt in range(MAX_SCHEMA_RETRIES + 1):
        built = build_messages(retries)
        if attempt == 0:
            purpose = "agent_final" if final_only else "agent_action"
        else:
            purpose = "schema_retry"
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
                action = AgentAction.model_validate(result.parsed)
                if final_only and action.action != "final":
                    error = "tool budget exhausted; action must be final"
                else:
                    return action, None
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
    tool_name: str,
    result: dict[str, Any],
    tool_call_id: int,
) -> dict[str, str]:
    return {
        "role": "user",
        "content": "UNTRUSTED_TOOL_DATA_JSON:\n"
        + json.dumps(
            {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                **result,
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
    transport: Transport = "telegram",
    is_private_chat: bool = True,
    attachments: Mapping[str, Path] | None = None,
) -> ChatReply:
    """Shared runtime production/eval; caller sở hữu lease cho tới khi hàm trả."""
    load_builtin_tools()
    settings = Settings()
    recorder = _TurnRecorder()
    direct_effects = _authorized_effects(text, transport, is_private_chat)
    required_web_tools = _required_web_tools(text)
    attempted_web_tools: set[str] = set()
    web_search_urls: tuple[str, ...] = ()
    paginated_documents: set[str] = set()
    attempted_effects: set[str] = set()
    effect_outcomes: dict[str, dict[str, Any]] = {}
    successful_effect_results: dict[str, ToolResult] = {}
    document_progress: dict[str, dict[str, int]] = {}
    completed_documents: set[str] = set()
    notify = progress or (lambda _message: None)
    turn_attachments = dict(attachments or {})

    def runtime_facts(final_only_reason: str | None = None) -> dict[str, Any]:
        facts = {
            "now_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "timezone": settings.timezone,
            "transport": transport,
            "required_effects": sorted(direct_effects),
            "effect_outcomes": effect_outcomes,
            "document_progress": document_progress,
            "completed_documents": sorted(completed_documents),
            "is_private_chat": is_private_chat,
            "attachment_ids": sorted(turn_attachments),
        }
        if required_web_tools:
            facts["required_web_tools"] = sorted(required_web_tools)
            facts["pending_web_tools"] = list(
                _pending_web_tools(required_web_tools, attempted_web_tools, web_search_urls)
            )
        if final_only_reason is not None:
            facts["final_only_reason"] = final_only_reason
        return facts

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
            runtime_facts=runtime_facts(),
        )

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation không tồn tại")
        summary = conversation.summary
        watermark = conversation.summary_until_message_id if context_strategy == "sprint3" else None
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
                    HISTORY_LIMIT - 1 if context_strategy == "window10" else SPRINT3_TAIL_MESSAGES
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
    context_summary = extractive_summary(summary, [], recent_tools) if recent_tools else summary
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
    force_document_final = False
    try:
        for step_index in range(MAX_STEPS + 1):
            if lease.cancel_event.is_set():
                reply_text = "Lượt xử lý đã được hủy trước bước tiếp theo."
                reply_text = _with_effect_receipts(reply_text, effect_outcomes)
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
            finalize_document = (
                force_document_final
                and not document_progress
                and not (direct_effects - attempted_effects)
            )

            def prepare(retries: list[MessageGroup]) -> ContextBuild:
                return _build_for_call(
                    strategy=context_strategy,
                    history_groups=[] if finalize_document else history_groups,
                    request=text,
                    execution_groups=execution_groups + retries,
                    summary=None if finalize_document else context_summary,
                    task_card=None if finalize_document else task_card,
                    runtime_facts=runtime_facts(
                        "document_pages_completed" if finalize_document else None
                    ),
                    max_chars=settings.context_max_chars,
                    include_tools=not finalize_document,
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
                final_only=finalize_document,
            )
            runtime_forced_web_tool = False
            required_web_action = _required_web_action(
                text,
                required_web_tools,
                attempted_web_tools,
                web_search_urls,
            )
            if required_web_action is not None and (
                action is None
                or action.action != "tool"
                or action.tool != required_web_action.tool
            ):
                action = required_web_action
                stop_reason = None
                runtime_forced_web_tool = True
            runtime_forced_document_page = False
            if (
                action is None
                and stop_reason == "required_context_too_large"
                and document_progress
                and step_index < MAX_STEPS
            ):
                attachment_id, progress_state = next(iter(document_progress.items()))
                action = AgentAction(
                    action="tool",
                    tool="read_document",
                    params={
                        "attachment_id": attachment_id,
                        "start_unit": progress_state["next_unit"],
                    },
                )
                stop_reason = None
                runtime_forced_document_page = True
            missing_effects = direct_effects - attempted_effects
            if (
                action is None
                and stop_reason == "required_context_too_large"
                and execution_groups
                and missing_effects
                and step_index < MAX_STEPS
            ):

                def prepare_effect_recovery(
                    retries: list[MessageGroup],
                ) -> ContextBuild:
                    return _build_for_call(
                        strategy=context_strategy,
                        history_groups=[],
                        request=text,
                        execution_groups=execution_groups + retries,
                        summary=None,
                        task_card=None,
                        runtime_facts=runtime_facts("required_effect_recovery"),
                        max_chars=settings.context_max_chars,
                        tool_names=frozenset(missing_effects),
                    )

                action, stop_reason = _next_action(
                    provider,
                    build_messages=prepare_effect_recovery,
                    record_result=record,
                    observer=context_observer,
                    strategy=context_strategy,
                )
            if action is None:
                overflow = stop_reason == "required_context_too_large"
                if overflow and execution_groups and not (direct_effects - attempted_effects):

                    def prepare_recovery(retries: list[MessageGroup]) -> ContextBuild:
                        return _build_for_call(
                            strategy=context_strategy,
                            history_groups=[],
                            request=text,
                            execution_groups=execution_groups + retries,
                            summary=None,
                            task_card=None,
                            runtime_facts=runtime_facts("context_overflow_recovery"),
                            max_chars=settings.context_max_chars,
                            include_tools=False,
                        )

                    recovered_action, _ = _next_action(
                        provider,
                        build_messages=prepare_recovery,
                        record_result=record,
                        observer=context_observer,
                        strategy=context_strategy,
                        final_only=True,
                    )
                    if recovered_action is not None:
                        reply_text = _with_effect_receipts(
                            recovered_action.final_answer or "",
                            effect_outcomes,
                        )
                        card = _persist_terminal(
                            conversation_id=conversation_id,
                            task_id=task_id,
                            step_index=step_index,
                            event="final_after_context_recovery",
                            task_status="completed",
                            reply_text=reply_text,
                            action=recovered_action,
                        )
                        terminal_written = True
                        if card:
                            notify(card)
                        return ChatReply(reply_text, recorder.summary())
                reply_text = (
                    "Yêu cầu và trạng thái bắt buộc vượt ngân sách context; "
                    "hãy rút ngắn yêu cầu hoặc tăng LAPLACE_CONTEXT_MAX_CHARS."
                    if overflow
                    else "Mô hình trả dữ liệu không hợp lệ nhiều lần nên lượt này dừng lại."
                )
                reply_text = _with_effect_receipts(reply_text, effect_outcomes)
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
            if (
                action.action == "final"
                and paginated_documents & completed_documents
                and not finalize_document
            ):

                def prepare_document_final(
                    retries: list[MessageGroup],
                ) -> ContextBuild:
                    return _build_for_call(
                        strategy=context_strategy,
                        history_groups=[],
                        request=text,
                        execution_groups=execution_groups + retries,
                        summary=None,
                        task_card=None,
                        runtime_facts=runtime_facts("document_pages_completed"),
                        max_chars=settings.context_max_chars,
                        include_tools=False,
                    )

                grounded_action, _ = _next_action(
                    provider,
                    build_messages=prepare_document_final,
                    record_result=record,
                    observer=context_observer,
                    strategy=context_strategy,
                    final_only=True,
                )
                if grounded_action is not None:
                    action = grounded_action

            if lease.cancel_event.is_set():
                reply_text = "Lượt xử lý đã được hủy sau khi bước hiện tại hoàn tất."
                reply_text = _with_effect_receipts(reply_text, effect_outcomes)
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

            if action.action == "final" and document_progress and step_index < MAX_STEPS:
                attachment_id, progress_state = next(iter(document_progress.items()))
                action = AgentAction(
                    action="tool",
                    tool="read_document",
                    params={
                        "attachment_id": attachment_id,
                        "start_unit": progress_state["next_unit"],
                    },
                )
                runtime_forced_document_page = True

            if action.action == "final":
                missing_effects = direct_effects - attempted_effects
                if missing_effects and step_index < MAX_STEPS:
                    execution_groups.append(
                        MessageGroup(
                            (
                                {
                                    "role": "user",
                                    "content": (
                                        "RUNTIME_REQUIRED_EFFECTS_JSON:\n"
                                        + json.dumps(
                                            {
                                                "missing": sorted(missing_effects),
                                                "instruction": (
                                                    "Call missing authorized tools before final."
                                                ),
                                            },
                                            ensure_ascii=False,
                                        )
                                    ),
                                },
                            )
                        )
                    )
                    continue
                if missing_effects:
                    reply_text = (
                        "Không thể hoàn tất thao tác reminder bắt buộc trong giới hạn lượt này."
                    )
                    event = "effect_incomplete"
                    task_status = "failed"
                else:
                    reply_text = _with_effect_receipts(
                        action.final_answer or "",
                        effect_outcomes,
                    )
                    event = "final"
                    task_status = "completed"
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event=event,
                    task_status=task_status,
                    reply_text=reply_text,
                    action=action,
                )
                terminal_written = True
                if card:
                    notify(card)
                return ChatReply(reply_text, recorder.summary())

            if step_index == MAX_STEPS:
                missing_effects = direct_effects - attempted_effects
                terminal_action: AgentAction | None = action
                if missing_effects:
                    reply_text = (
                        "Không thể hoàn tất thao tác reminder bắt buộc trong giới hạn lượt này."
                    )
                    event = "effect_incomplete"
                    task_status = "failed"
                else:

                    def prepare_final(retries: list[MessageGroup]) -> ContextBuild:
                        return _build_for_call(
                            strategy=context_strategy,
                            history_groups=[],
                            request=text,
                            execution_groups=execution_groups + retries,
                            summary=None,
                            task_card=None,
                            runtime_facts=runtime_facts("tool_budget_exhausted"),
                            max_chars=settings.context_max_chars,
                            include_tools=False,
                        )

                    forced_action, _ = _next_action(
                        provider,
                        build_messages=prepare_final,
                        record_result=record,
                        observer=context_observer,
                        strategy=context_strategy,
                        final_only=True,
                    )
                    if forced_action is not None:
                        terminal_action = forced_action
                        reply_text = _with_effect_receipts(
                            forced_action.final_answer or "",
                            effect_outcomes,
                        )
                        event = "final_after_tool_budget"
                        task_status = "completed"
                    else:
                        reply_text = (
                            f"Đã chạm giới hạn {MAX_STEPS} bước công cụ mà chưa xong; "
                            "lượt này dừng để tránh chạy vô hạn."
                        )
                        reply_text = _with_effect_receipts(reply_text, effect_outcomes)
                        event = "step_limit"
                        task_status = "step_limit"
                card = _persist_terminal(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_index=step_index,
                    event=event,
                    task_status=task_status,
                    reply_text=reply_text,
                    action=terminal_action,
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

            if task_id is None:
                raise RuntimeError("tool task was not created")
            tool_name = action.tool or ""
            cached_result = successful_effect_results.get(tool_name)
            replayed = cached_result is not None
            notify(
                f"Bước {step_index + 1}: "
                + (
                    f"dùng lại kết quả {tool_name} đã hoàn tất..."
                    if replayed
                    else f"chạy tool {tool_name}..."
                )
            )
            tool_context = ToolContext(
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                transport=transport,
                is_private_chat=is_private_chat,
                conversation_id=conversation_id,
                request_message_id=request_message_id,
                task_id=task_id,
                step_index=step_index,
                now_utc=datetime.now(UTC),
                timezone=settings.timezone,
                attachments=turn_attachments,
                authorized_effects=direct_effects,
            )
            result = (
                cached_result.model_copy(update={"latency_ms": 0})
                if cached_result is not None
                else execute(tool_name, action.params, tool_context)
            )
            if tool_name in {"web_search", "fetch_page"}:
                attempted_web_tools.add(tool_name)
            if tool_name == "web_search" and result.ok:
                web_search_urls = tuple(dict.fromkeys((*web_search_urls, *result.sources)))
            if tool_name in direct_effects:
                attempted_effects.add(tool_name)
                effect_outcomes[tool_name] = _effect_outcome(result)
                if result.ok:
                    successful_effect_results[tool_name] = result
            if tool_name == "read_document" and result.ok and isinstance(result.data, dict):
                document = result.data.get("document")
                attachment_id = (
                    document.get("attachment_id") if isinstance(document, dict) else None
                )
                if isinstance(attachment_id, str):
                    if result.data.get("complete") is True:
                        document_progress.pop(attachment_id, None)
                        completed_documents.add(attachment_id)
                        if runtime_forced_document_page:
                            force_document_final = True
                    else:
                        completed_documents.discard(attachment_id)
                        next_unit = result.data.get("next_unit")
                        total_units = document.get("total_units")
                        if isinstance(next_unit, int) and isinstance(total_units, int):
                            document_progress[attachment_id] = {
                                "next_unit": next_unit,
                                "total_units": total_units,
                            }
                            paginated_documents.add(attachment_id)
            envelope = result.model_dump(mode="json")
            preview = (
                json.dumps(result.data, ensure_ascii=False, sort_keys=True)
                if result.ok
                else result.error or ""
            )
            with session_scope() as session:
                tool_call = repo.record_tool_call(
                    session,
                    task_id=task_id,
                    tool_name=tool_name,
                    params_json=json.dumps(action.params, ensure_ascii=False),
                    result_json=json.dumps(envelope, ensure_ascii=False),
                    ok=result.ok,
                    error=result.error,
                    latency_ms=result.latency_ms,
                )
                observation = (
                    bounded_tool_observation(
                        tool_name=tool_name,
                        result=envelope,
                        tool_call_id=tool_call.id,
                        limit=(
                            DOCUMENT_TOOL_OBSERVATION_MAX_CHARS
                            if tool_name == "read_document"
                            else TOOL_OBSERVATION_MAX_CHARS
                        ),
                    )
                    if context_strategy == "sprint3"
                    else _full_observation(
                        action.tool or "",
                        envelope,
                        tool_call.id,
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
                            "error_code": result.error_code,
                            "sources": result.sources,
                            "preview": preview[:200],
                            "replayed": replayed,
                        },
                        "task_state": _task_state(action),
                        "stop_reason": None,
                        "replayed": replayed,
                        "runtime_forced_document_page": runtime_forced_document_page,
                        "runtime_forced_web_tool": runtime_forced_web_tool,
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
                        "tool": tool_name,
                        "ok": result.ok,
                        "error_code": result.error_code,
                        "replayed": replayed,
                        "runtime_forced_document_page": runtime_forced_document_page,
                        "runtime_forced_web_tool": runtime_forced_web_tool,
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
    transport: Transport = "telegram",
    is_private_chat: bool = True,
    attachments: Mapping[str, Path] | None = None,
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
            transport=transport,
            is_private_chat=is_private_chat,
            attachments=attachments,
        )
    finally:
        release_turn(lease)


def handle_message(
    telegram_user_id: int,
    username: str | None,
    text: str,
    progress: Callable[[str], None] | None = None,
    *,
    transport: Transport = "telegram",
    is_private_chat: bool = True,
    attachments: Mapping[str, Path] | None = None,
) -> ChatReply:
    """Reserve one user turn and run it with explicit trusted transport facts."""
    lease = try_reserve_turn(telegram_user_id)
    if lease is None:
        return ChatReply("Đang có một yêu cầu khác của bạn được xử lý. Hãy chờ hoặc dùng /cancel.")
    return _run_reserved_message(
        lease,
        telegram_user_id,
        username,
        text,
        progress,
        transport=transport,
        is_private_chat=is_private_chat,
        attachments=attachments,
    )
