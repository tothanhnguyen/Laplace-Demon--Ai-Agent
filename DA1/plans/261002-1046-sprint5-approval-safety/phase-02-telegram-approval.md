---
title: "Phase 2: Approval bền và Telegram pause/resume"
status: pending
priority: P0
effort: "20h"
dependsOn: [phase-01-risk-policy-gateway]
---

# Phase 2: Approval bền và luồng Telegram pause/resume

## Context và yêu cầu

[Plan tổng](./plan.md) · [Policy](./phase-01-risk-policy-gateway.md) · [Budget](./phase-03-execution-budgets.md). S5-03; hoàn thiện mutation gateway S5-02.

Baseline: `models.py:61-106` chưa có approval; `repo.py:235-240` chỉ close task đang `running`; `bot/handlers.py:312-373` chạy sync shared runtime bằng `to_thread` + shield; `services/memory.py:13-60` lease theo **Telegram ID**, khác DB owner ID. Handler hiện xóa uploads khi worker kết thúc; không thể pause rồi dùng lại path đã xóa.

Decision: **một bảng approval bền + checkpoint process-local có bounded lifetime; worker trả paused và release lease.** Callback resume đúng shared runtime với cùng task/request, không gọi `handle_message(original_text)` lại. Restart fail-closed, không task recovery của Sprint 6. Không giữ thread, transaction hoặc lease suốt 15 phút chờ.

## Persistence và state machine

Create `ToolApproval` / bảng `tool_approvals`, không nhét authority vào `Step.detail` hoặc chỉ boolean trên Task:

- `id`, unique random `token`/generation, `user_id`, `task_id`, `step_index`, `tool_name`, `risk_level`.
- `params_json`: private prepared action; `public_args_hash`: SHA-256 canonical validated public args (dùng đối chiếu, không thực thi); `resource_fingerprint` cho update/cancel; `preview_json` chứa old/effective facts và policy/schema version.
- `status`: `pending`, `approved`, `executed`, `rejected`, `expired`, `cancelled`, `invalidated`, `failed`, `interrupted`.
- UTC `created_at`, `expires_at`, `decided_at`, `consumed_at`; private `chat_id`, `preview_message_id`; canonical `result_json`/effect receipt cho mutation đã commit.
- FK user/task; index `(status, expires_at)` và `(user_id, status)`; tối đa một pending approval/task, một paused task/user. New token mỗi proposal, không dùng row ID đơn lẻ vì SQLite ID có thể được reuse sau forget.
- Dùng UTC epoch seconds cho approval timestamps, random token ≥128 bits; callback token không ghi operational log. Partial unique index trên user/task với status `pending` hoặc `approved`; expiry query giới hạn 25 rows/lượt, tick 1 giây qua scheduler hiện có. SQL callback luôn kiểm deadline độc lập với tick.

| Event | Approval trước → sau | Task trước → sau | Cleanup |
|---|---|---|---|
| Publish proposal | mới → pending | running → awaiting_approval | Transfer file/state sang checkpoint trước return. |
| Owner approve, claim thắng | pending → approved | awaiting_approval → running | Worker nhận lease; chưa xóa checkpoint/files. |
| Commit mutation T3 | approved → executed | running → running | Giữ receipt; loop tiếp tục hoặc kết thúc. |
| Owner reject / TTL / cancel khi pending | pending → rejected / expired / cancelled | awaiting_approval → rejected / expired / cancelled | DB commit trước, rồi cleanup RAM/files. |
| Preview/binding lỗi | pending → invalidated | awaiting_approval → failed | Cleanup, không xin model chạy tiếp. |
| Target stale sau claim | approved → invalidated | running → failed | Không write; yêu cầu user gửi yêu cầu mới. |
| Cancel trước T3 | approved → cancelled | running → cancelled | Không write, release bởi worker. |
| Tool/validation lỗi trước commit | approved → failed | running → failed | Rollback T3, ghi terminal trong transaction riêng. |
| Không launch được worker / restart | pending hoặc approved → interrupted | awaiting_approval hoặc running → interrupted | Không replay; release lease chưa transfer, cleanup. |
| Cancel/error sau T3 | executed giữ nguyên | running → cancelled / failed / interrupted | Báo receipt đã commit; không giả rollback effect. |

Chuyển approval + task trong cùng transaction với allowed-source states rõ ràng. `approved` chưa phải effect thành công; `executed` không được đổi thành failed vì reply lỗi. `close_task` hiện chỉ đóng `running`, nên pending transitions phải có repo helper riêng.

Migration theo `db.py` hiện hữu: tạo `tool_approvals` và indexes; thêm nullable FK `ToolCall.approval_id` + unique index (legacy rows NULL). Không cần cột Task mới trong Phase 2; status string hiện có đủ dùng. Init lần hai idempotent, không rebuild/reset DB. Reminder rows trước Sprint 5 giữ nguyên theo consent Sprint 4; mọi tạo/sửa/hủy mới dùng approval mới.

### Dữ liệu tối thiểu, không dùng hai nguồn sự thật

| Dữ liệu | Nơi sở hữu | Quy tắc |
|---|---|---|
| `PreparedReminder` | Typed value trong `tools/reminders.py`, serialize vào `ToolApproval.params_json` | `operation`, `reminder_id` nếu có, `message_changed`, `time_changed`, effective `message`, `due_at_utc`, `timezone`. Flags giữ **ý định update**; không truyền effective due vào repo nếu `time_changed=false`. |
| Snapshot mục tiêu | `ToolApproval.resource_fingerprint` | Hash canonical JSON của `id,user_id,status,message,due_at_utc,timezone`; không hash `next_attempt_at_utc`/`updated_at` vì scheduler defer có thể đổi chúng. Status đã đổi rồi quay về cùng values không thay nội dung được duyệt; atomic T3 vẫn kiểm current allowed status. |
| Quyết định/receipt | `ToolApproval` trong DB | DB là authority; callback không mang params, memory không tự ghi `approved`. |
| `TurnState` và checkpoint map | `services/chat.py` | Chuyển locals của loop thành một state; checkpoint giữ state này, không copy sang một engine resume khác. |
| Approval lifecycle | `services/approvals.py` + repo helpers | Module này không import `services/chat.py`; handler gọi lifecycle rồi gọi chat resume. Tránh circular import. |
| `TurnBudget` | Phase 3, giữ trong `TurnState` | Phase 2 giữ counters/recorder hiện có, không tạo budget class giả hoặc reset khi resume. |

Prepared schema là nội bộ, **không thay** `CreateReminderArgs`/`UpdateReminderArgs` công khai bằng field UTC trusted. Giữ semantics Sprint 4: update text chỉ trên `pending`; reschedule được phép trên `pending`, `failed`, `delivery_unknown` với future due; cancel chỉ trên `pending`. Không ghi old state trở lại sau khi target đã thay đổi.

### Mỗi transaction làm đúng một việc

| Bước | Transaction / I/O | Nếu lỗi hoặc crash |
|---|---|---|
| T1: publish proposal | DB lưu pending + task awaiting; checkpoint ready. Gửi preview **không nút**, lưu `preview_message_id`, rồi edit gắn nút. Không giữ DB session qua Telegram I/O. | Send/bind/edit lỗi → invalidate; callback không thấy đủ binding → deny. |
| T2: claim decision | Đã giữ lease; CAS pending chưa hết hạn + đúng actor/binding → approved; task → running. | Chưa thắng CAS thì không worker/effect; thất bại launch worker → interrupted, release lease đúng owner. |
| T3: commit effect | Trong session của reminder: recheck approved + owner + fingerprint + cancel/deadline → write → validate output → lưu receipt + executed → commit. | Trước commit rollback tất cả; sau commit coi effect đã xảy ra, không chạy lại dù reply/log thất bại. |
| T4: record observation | Chat lấy receipt, persist ToolCall/Step với approval ID; unique nullable `ToolCall.approval_id` ngăn duplicate audit row. | Crash sau T3 trước T4: startup đánh task interrupted và giữ receipt, không replay effect. |

Repo helpers nhận Session và không commit như convention hiện có. Approval consume thuộc đúng transaction T3, không mở nested session trong helper. `spec.run` của reminder tải prepared payload bằng approval ID đã bind vào trusted context; không resolve lại model args.

## Snapshot và preview đúng effect

1. Với create hoặc update có đổi lịch: resolve thời gian một lần thành UTC, lưu `time_changed=true`; lúc approve nếu new due không còn ở tương lai thì invalidate, không tự lùi giờ. Cancel và update chỉ đổi text giữ semantics Sprint 4: kiểm owner/status/fingerprint, **không từ chối chỉ vì old due đã qua**.
   Due UTC là prepared field nội bộ; mutation không resolve delay lần hai, không đưa timestamp/capability trusted vào public model schema.
2. Create preview: toàn bộ message (≤1.000 chars), private recipient, local date/time + timezone + UTC, tool/risk, expiry. Update: old → new effective message/due, reminder ID. Cancel: ID/content/due và hành động sẽ hủy.
3. Preview plain text gồm đầy đủ old/new message (mỗi message ≤1.000 chars) và metadata cố định. Render một message nếu fit giới hạn Telegram; thử cả ký tự Unicode ngoài BMP. Nếu không fit thì từ chối proposal với lý do rõ, không truncate material payload hoặc tự tách nhiều message. Runtime credentials từ Settings/headers không được chèn vào effect; user-provided content giữ nguyên trong private preview.
4. Lưu snapshot target state/fingerprint; callback/tool recheck owner, status, due và fingerprint. Target thay đổi hoặc dispatcher đã claim → không áp approval cũ, không hidden replan. Params/tool/risk mới cần generation mới.
5. Pending row + awaiting task + checkpoint đã sẵn sàng trước expose keyboard; publication và cancel phải serialize. Preview send lỗi hoặc checkpoint publication lỗi → invalidate proposal, cleanup, không chạy tool.

## Pause/resume và attachment ownership

`services/approvals.py` chỉ lifecycle/repo operations. `TurnState` và checkpoint map ở `services/chat.py`: giữ task/conversation/request IDs, **suspended AgentAction + original step_index + task_update + forced-action flags**, bounded history/execution groups, receipts, attempted/required tools, document progress, attachment bundle, recorder và counters. Resume thực thi prepared action tại original index → chạy post-tool bookkeeping một lần → chuyển next decision index; không gọi LLM trước approved tool và không tính pause thành tool thành công. Không ORM Session/provider socket trong checkpoint.

- `ChatReply` thêm typed pause metadata; chat lưu paused step, không `_persist_terminal(...completed)` và không ghi final success. Worker return → wrapper release lease đúng ownership.
- Callback acquire lease mới → load/check proposal và checkpoint → resume cùng shared loop; approved immutable tool chạy **trước** decision LLM tiếp theo. Không model tự viết lại args hay gọi lại effect đã xong.
- Reminder mutation trong session hiện có phải verify/consume approval **cùng transaction** với reminder write và canonical receipt; validate result schema trước commit. Gateway claim đã committed ngăn callback thứ hai; inner write dùng approved row/params chứ không tên-tool capability. Crash trước commit rollback toàn write/consume; crash sau commit có receipt và không replay. Runtime dùng receipt đó để ghi ToolCall/Step, không duplicate side effect. Không giữ transaction qua LLM/network.
- Giữ `ToolContext` plain immutable IDs/capabilities, không truyền SQLAlchemy Session qua thread. Migrate stateful tool/session persistence cần thiết; không executor cũ cho tests/CLI.
- **Worker** chuyển ownership attachment bundle sang checkpoint đồng bộ với pause publication, trước return. Normal cleanup và detached-worker completion callback đều kiểm cùng ownership marker, không phụ thuộc handler có nhận được `ChatReply.pause` hay không. Resume giữ ownership tới worker terminal hoặc pause tiếp. Pending tối đa 900s; executing tới terminal trong active budget. Cleanup idempotent khi terminal/shutdown; startup dọn orphan chỉ trong managed root, không follow symlink ra ngoài.
- User gửi request mới khi paused: báo đang chờ duyệt, cho `/memory`, `/cancel`, `/forget`, approval callback; không chạy turn mới ghi lên checkpoint. Reminder dispatcher vẫn có thể gửi lịch đã duyệt khác vì pending không giữ lease.
- New chat/document admission phải kiểm paused state và reserve `TurnLease` trong **cùng synchronization boundary** trước download; truyền lease đã reserve vào worker, không reserve lần hai. Publication dùng cùng guard để không có check-then-reserve race. Upload path unique theo request/turn; request bị chặn không download/overwrite checkpoint. Download failure release lease + cleanup; reminder/forget/approval-resume dùng admission riêng.

## Telegram và concurrency ordering

- Private `InlineKeyboardMarkup`: **Xác nhận / Hủy**. `callback_data` chỉ decision + opaque token, trong 64 bytes; không args/owner ID/secret/path. [Telegram API](https://core.telegram.org/bots/api#inlinekeyboardbutton).
- Derive actor từ `callback.from_user.id`, map DB User; compare stored owner, private chat, preview message binding, expiry, policy version và checkpoint generation. Không tin callback text/model hoặc chat ID thay actor. Reject inaccessible/missing/inline-origin message không đủ binding.
- `bot/middleware.py` hiện chỉ nhận Message: callback cần identity/rate limiting phù hợp, không reuse middleware khiến callback bị drop. Luôn `callback.answer()` cả deny/stale/busy; [aiogram contract](https://docs.aiogram.dev/en/latest/api/types/callback_query.html).
- Approve ordering: acquire `TurnLease(kind=chat)` → DB CAS `pending → approved` + task running → resume worker với shield → release chỉ khi worker thật kết thúc. Busy: giữ pending và cho bấm lại, không consume trước acquire.
- Reject/expiry/cancel: CAS thắng trước → terminal task và remove checkpoint/files → best-effort remove keyboard. DB là authority, keyboard editing không phải lock. Approve/reject đồng thời chỉ một winner.
- `/cancel`: ngoài cooperative running worker, cancel pending của owner. Worker kiểm cancel ngay trước publication và ngay trước mutation; serialize publish→release window, không trả “không có yêu cầu” rồi để approval sống. Nếu write đã commit, báo effect đã thực hiện, không giả rollback.
- `/forget confirm`: giữ lease kind=forget; xóa `ToolCall` (FK approval) trước `ToolApproval`, rồi Task theo child-first transaction; detach usage/request links trước xóa Messages. DB commit thành công mới remove checkpoint và unlink files; DB rollback giữ checkpoint nguyên vẹn. Unlink lỗi: quyền callback vẫn đã bị revoke, báo cleanup chưa trọn vẹn và giữ file trong managed-root cleanup, không giả atomic giữa SQLite/filesystem. Approve thắng lease → forget busy; forget thắng → callback stale không tái tạo dữ liệu.

## TTL, startup và shutdown

`LAPLACE_APPROVAL_TIMEOUT_SECONDS=900`, validated finite; `expires_at=created+TTL`, click đúng/qua deadline bị reject. Periodic bounded expiry scan reuse scheduler lifecycle hiện có; deadline check trong CAS vẫn bắt buộc. Không tạo một sleeping task cho mỗi proposal.

Startup trước polling/dispatcher: pending/approved chưa commit receipt → interrupted; proposal executed giữ receipt, task chưa terminal → interrupted **không replay**. Task awaiting không có checkpoint không thể resume. Không áp reminder requeue/recovery lên approval.

Shutdown: ngừng nhận/resume approval, dừng expiry job, cooperative cancel + drain running chat workers trong I/O deadlines, invalidate pending và cleanup managed files/checkpoints, drain reminder scheduler, rồi đóng bot. Không release lease từ handler cancel khi thread vẫn chạy.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Create | `laplace/services/approvals.py` | Lifecycle/repo orchestration và expiry; checkpoint thuộc chat, không circular import. |
| Modify | `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` | Table/index/migration, CAS transitions, atomic mutation receipt, snapshots và child-first forget. |
| Modify | `laplace/services/chat.py`, `laplace/tools/base.py`, `laplace/tools/reminders.py` | Pause outcome, same-loop continuation và consent consume trong mutation transaction. |
| Modify | `laplace/services/memory.py`, `laplace/services/reminders.py` | Pending cancel/forget serialization; expiry lifecycle giữ delivery invariants. |
| Modify | `laplace/bot/handlers.py`, `laplace/bot/middleware.py`, `laplace/bot/runner.py` | Preview/callback identity, shared worker lifecycle, attachment ownership, startup/shutdown. |
| Modify | `laplace/context.py`, `laplace/config.py`, `.env.example`, `README.md` | Waiting card, TTL, retained-upload disclosure và usage help. |
| Modify | `tests/test_db.py`, `tests/test_bot_handlers.py`, `tests/test_bot_runner.py`, `tests/test_session_memory.py`, `tests/test_reminders.py`, `tests/test_chat_service.py` | Existing migration/ownership/cancel/delivery contracts. |
| Create | `tests/test_approvals.py` | Snapshot, CAS, TTL equality, replay, stale-resource, restart và attachment cleanup boundaries. |


## Thứ tự code

| ID | Việc cần làm | Đầu ra |
|---|---|---|
| P2-01 | ORM/migration/index + repo CAS/paired transitions. | DB cũ nâng cấp hai lần an toàn; bảng transition có checks. |
| P2-02 | `PreparedReminder`, old/new preview và T3 atomic consume/write/validate/receipt. | Create/update/cancel đúng semantics và không duplicate write. |
| P2-03 | Extract `TurnState`, pause/resume cùng loop; admission + attachment ownership. | Same task/request/cursor; detached handler không xóa file đang pause. |
| P2-04 | Preview publication T1, private callbacks/T2, middleware identity và fast callback answer. | Approve/reject/wrong owner/replay/busy đều có phản hồi đúng. |
| P2-05 | Cancel/forget, expiry scan, startup invalidation và shutdown drain. | Không row pending mắc kẹt; không auto replay; cleanup không phá DB rollback. |
| P2-06 | Migration/race/handler smoke + gate G2. | DB/receipt/file ownership là oracle, không wording. |

**API bàn giao:** chat nhận first-turn hoặc resume-token qua entrypoints riêng nhưng gọi chung loop; lifecycle service không gọi LLM. Gateway nhận approval ID trong trusted context, load stored payload và kiểm lại task/user/step. `ToolCall.approval_id` chỉ dùng cho execution đã có receipt, không cho proposal đang chờ.

## Checklist và gate G2

- [ ] Migration cũ + init hai lần; không mất reminders/history/usage, FK hợp lệ.
- [ ] Create/update/cancel không mutate trước approve; chạy đúng immutable preview khi approve.
- [ ] Reject/expiry/wrong owner/replay/double-click không side effect; busy giữ pending.
- [ ] Cancel/forget/publication/claim races đúng lease/CAS; checkpoint/files cleanup mọi terminal.
- [ ] Relative time và stale target không silent change; không reset task/request/counters khi resume.
- [ ] Restart fail-closed và shutdown drain, không automatic execution/replay.
- [ ] Smoke qua actual handlers/shared runtime + DB tạm: pause → approve → real reminder row → dispatcher; reject/expiry no row. Telegram live ở Phase 5.

## Rủi ro và handoff

P0: tool mở transaction riêng làm consent/receipt rời write → crash duplicate. Refactor đúng reminder session boundary, không test-only approval bypass. P0: upload bị xóa sớm/rò sau 15 phút → explicit ownership transfer + cleanup cases. P0: giữ thread/lease khi chờ → starvation scheduler/forget. Phase 3 dùng checkpoint này cho cumulative budgets, không cấp budget mới mỗi click.
