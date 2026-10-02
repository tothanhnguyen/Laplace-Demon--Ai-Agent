---
title: "Phase 4: Owner-safe reminder scheduler"
status: completed
priority: P0
effort: "16h"
dependsOn: [phase-01-tool-contract-runtime]
---

# Phase 4: Reminder domain và scheduler phục hồi restart

Status: Completed | Priority: P0 | Effort: 16h | Depends on: Phase 1

## Mục tiêu

Private Telegram user tạo/cập nhật/hủy reminder của chính mình; dữ liệu nằm trong SQLite, process restart không làm mất reminder tương lai, scheduler gửi đúng user và không nói `sent` khi Telegram outcome chưa quan sát được.

- [Plan tổng](./plan.md) · [Contract](./phase-01-tool-contract-runtime.md) · [Telegram acceptance](./phase-05-telegram-acceptance.md)
- Baseline: UTC helper `models.py:9-11`, owner `User.telegram_user_id` `models.py:19-27`, short transaction `db.py:76-89`, per-user lease `services/memory.py:26-92`, bot lifecycle `bot/runner.py:21-47`.
- APScheduler chọn `AsyncIOScheduler` vì aiogram chạy asyncio: <https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html>.

## Persistence contract

### R1 — `Reminder` table

Không overload Agent `Task`. Add một model:

| Field | Contract |
|---|---|
| `id` | Integer PK, trả cho owner để update/cancel |
| `user_id` | Required FK users; authorization boundary duy nhất |
| `message` | Nonblank, bounded dưới Telegram single-message limit |
| `timezone` | Valid IANA zone dùng display/validation |
| `due_at_utc` | Unix epoch seconds, unambiguous và sortable |
| `next_attempt_at_utc` | Epoch cho first attempt/retry; scheduler query field này |
| `attempt_count` | Tăng atomically khi claim; tối đa 3 attempts |
| `status` | `pending | delivering | sent | cancelled | failed | delivery_unknown | missed` |
| `first_attempt_at_utc`, `last_attempt_at_utc`, `sent_at_utc` | Timing/SLA và durable lifecycle evidence |
| `telegram_message_id` | Chỉ set khi Bot API trả success |
| `last_error` | Bounded, sanitized |
| `created_at`, `updated_at` | Existing UTC convention |

Indexes `(status,next_attempt_at_utc,id)` và `(user_id,status,due_at_utc)`. `Base.metadata.create_all()` tạo table mới trên DB cũ; migration regression phải chạy init hai lần, giữ rows Sprint 2/3 và pass `PRAGMA foreign_key_check`.

### R2 — Tool contracts

Ba tools explicit trong `laplace/tools/reminders.py`:

- `create_reminder(message, run_at?, delay_seconds?)`
- `update_reminder(reminder_id, message?, run_at?, delay_seconds?)`
- `cancel_reminder(reminder_id)`

Rules:

- Exactly one absolute `run_at` RFC3339 có numeric offset hoặc relative `delay_seconds`; create bắt buộc, update time optional nhưng phải đổi ít nhất một field.
- Runtime facts cung cấp `now_utc` và configured `LAPLACE_TIMEZONE` (default `Asia/Ho_Chi_Minh`); không dựa model training date hoặc host local timezone.
- Absolute offset phải khớp configured IANA zone tại instant đó; nonexistent DST time reject, repeated hour được offset disambiguate.
- Normalize/store epoch UTC; require future, max horizon bounded; message length bounded.
- Context phải `transport=telegram` và private chat. CLI/group trả `forbidden`/`unsupported transport`, không insert row.
- Owner và destination không có trong args. Update/cancel query `id AND user_id`; other-owner và nonexistent cùng trả `not_found`.
- Runtime hiện tại thực thi mỗi accepted action đúng một lần; không thêm idempotency key giả dựa trên Task/Message ID có thể bị tái sử dụng sau `/forget`. Nếu sau này có queue replay, execution ID durable phải được thiết kế ở Sprint 6.
- `sent`, `cancelled`, `missed` immutable. `failed`/`delivery_unknown` chỉ rearm khi owner update với future time; `delivering` trả conflict.

Risk metadata medium cho create/update/cancel. Sprint 4 không có Approval Gate;
interim invariant: capability chỉ được mint từ directive rõ ràng `/remind`,
`/remind_update`, `/remind_cancel` trong private chat, luôn self-destination.
Web/document text và câu văn mô tả không được tự kích hoạt reminder.

## Scheduler design

### R3 — Một source of truth

Dùng `APScheduler>=3.11,<4` `AsyncIOScheduler` với **MemoryJobStore** cho một interval job `dispatch_due_reminders`:

- Reminder rows trong app table là durable truth và được query mỗi tick.
- Defaults: interval 1 giây, batch 25, tối đa 5 deliveries đồng thời, max 3 attempts, Telegram send timeout 10 giây, misfire/retry grace 3.600 giây.
- Interval job được tạo lại startup với fixed ID, `replace_existing=true`, `coalesce=true`, `max_instances=1`.
- Không dùng `SQLAlchemyJobStore`: nó serialize callable/args, tạo source of truth thứ hai, khó owner-query/wipe và tăng coupling version. Requirement restart được chứng minh bằng app rows + startup recovery, không bằng opaque pickled jobs.
- Single bot process là deployment invariant; conditional claim chặn hai scans cùng gửi đồng thời.

### R4 — Due dispatch và transaction

1. Query tối đa 25 rows `pending AND next_attempt_at_utc <= now`, ordered/indexed; join User chỉ để lấy trusted Telegram ID.
2. Acquire `try_reserve_turn(telegram_user_id, kind="reminder")` **trước claim**. Busy bởi chat/forget → leave pending cho tick sau.
3. Conditional update `pending → delivering`, set first/last attempt timestamps và increment `attempt_count`; rowcount 1 mới sở hữu send. Commit trước network.
4. `await bot.send_message(user.telegram_user_id, formatted_message_with_stable_reminder_id)` không giữ DB transaction; timeout 10 giây.
5. Success response → new transaction `delivering → sent`, set actual Telegram message ID/time.
6. Permanent Telegram rejection (blocked/invalid chat/auth) → `failed`.
7. 429 honors bounded `retry_after`; 5xx/network/timeout/cancellation và stale crash claim là ambiguous. Nếu `attempt_count < 3` và `now <= due_at + grace`, commit `delivering → pending` với bounded backoff/`next_attempt_at`; nếu hết attempts/grace, commit `delivery_unknown`.
8. Hold lease through sent/failed/requeue/unknown commit; release in `finally`.

Telegram và SQLite không có distributed transaction/idempotency key chung. Contract là **at-least-once trong retry budget**: crash sau Telegram accept nhưng trước DB commit có thể gửi trùng khi recovery. Notification luôn chứa stable reminder ID để user nhận biết; không được đổi ambiguity thành false `sent` hoặc bỏ stale claim im lặng.

### R5 — Startup, misfire, shutdown

Startup order:

1. Validate token; `init_db()`.
2. Construct Bot.
3. Recovery transaction xét từng stale `delivering`: còn attempts và còn grace → `pending` với `next_attempt_at=now`; hết budget → `delivery_unknown`. Future pending giữ nguyên.
4. Start scheduler; first tick xử lý pending due/retry.
5. Start Dispatcher polling.

Misfire policy:

- Pending overdue/retry ≤ `LAPLACE_REMINDER_MISFIRE_GRACE_SECONDS=3600` được gửi ngay; message ghi scheduled time và “gửi trễ”.
- Pending overdue quá grace → `missed`, không gửi stale notification.
- Acceptance restart cover future reminder, downtime crossing due trong grace và process death sau claim.
- Healthy/unloaded single-reminder SLA: `first_attempt_at_utc - due_at_utc <= 2s`; observed Telegram success ≤12s sau due. Retry/misfire không được báo là on-time.

Shutdown/failure order:

1. Stop/wake interval job; await active dispatch trong send timeout.
2. In-flight cancellation đi qua cùng retry/unknown policy rồi release lease.
3. Shutdown AsyncIOScheduler.
4. Close Bot session cuối cùng.

Cùng sequence khi `start_polling()` raise. Không coroutine dùng Bot sau session close.

## `/memory` và `/forget`

- `/memory` private-only thêm total/active counts và tối đa 3 pending reminders gồm ID, localized due time, bounded message để user có ID update/cancel.
- `/forget` pre-confirm warning và success response nói rõ xóa pending/historical
  reminders, usage được giữ; file upload tạm đã bị xóa sau document turn và
  không thuộc memory.
- `wipe_session()` delete reminders của `user.id` trong cùng transaction; usage before/after không đổi; repeated forget idempotent.
- Shared lease serializes delivery với forget: forget thắng → row bị xóa trước claim; delivery thắng → forget trả busy cho tới sent/failed/requeue/unknown commit. Busy text nói “đang có thao tác tài khoản”.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/models.py`, `laplace/db.py` | Reminder model/indexes, additive migration evidence |
| Modify | `laplace/repo.py` | Owner CRUD, due query, conditional claim/finalize/recovery, wipe/stats |
| Create | `laplace/tools/reminders.py` | Typed create/update/cancel + time validation |
| Create | `laplace/services/reminders.py` | Scheduler-independent dispatch service + APScheduler lifecycle wrapper |
| Modify | `laplace/services/chat.py`, `laplace/prompts.py` | Trusted context/current time/direct-intent rules |
| Modify | `laplace/services/memory.py`, `laplace/bot/handlers.py` | Reminder lease, memory preview, truthful forget |
| Modify | `laplace/bot/runner.py` | Scheduler startup/shutdown around polling |
| Create | `tests/test_reminders.py`, `tests/test_bot_runner.py` | Domain/state/time/lifecycle proofs |
| Modify | `tests/test_session_memory.py`, `tests/test_chat_service.py`, `tests/test_bot_handlers.py` | Ownership/forget/runtime integration |

## Đầu việc

| ID | Việc | Effort | Gate |
|---|---|---:|---|
| S4-05A | Reminder model/repo/time normalization/retry fields | 5h | Owner-scoped, short transactions, UTC epoch |
| S4-05B | Register create/update/cancel tools | 3h | No model owner/destination; terminal rules |
| S4-05C | AsyncIOScheduler dispatcher + retry/recovery/lifecycle | 5h | Lease-before-claim, no DB lock over network, Bot close last |
| S4-05D | Memory/forget integration + failure regressions | 3h | Privacy race, usage and timing invariants |

## Nghiệm thu G4

Dùng fake clock/delivery callback, không sleep:

- Create stores normalized epoch/zone/owner; other-owner update/cancel indistinguishable from missing.
- Naive timestamp, unknown zone, offset mismatch, past/far-future, both/neither absolute-relative inputs create no row.
- Update pending keeps ID; cancel idempotent; terminal rows immutable; hai yêu cầu create trực tiếp là hai reminders có chủ đích.
- Concurrent/repeated ticks chỉ có một live claim; DB transaction không held trong blocked fake delivery.
- Success stores returned Telegram message ID; permanent reject failed; 429 honors retry_after; timeout/5xx/stale delivering requeues within budget rồi becomes delivery_unknown if exhausted.
- Crash-after-claim restart requeues and attempts delivery; test thừa nhận duplicate possible if first Telegram send actually succeeded.
- Same temp SQLite: future pending remains; due-in-grace sends/retries; over-grace becomes missed.
- Fake clock asserts healthy single reminder first-attempt ≤2s and success ≤12s; overdue/retry carries late marker.
- CLI/group call inserts nothing.

Smoke không-test: create reminder row through shared mocked agent turn, destroy scheduler/process objects, reconstruct against same DB, advance fake clock, invoke actual due dispatcher and observe one fake Telegram delivery + `sent` state.

## Không thuộc phase

Không recurring/cron reminders, calendar, arbitrary recipients/group delivery, notification channels khác, Approval Gate UI, distributed locks/multi-worker scheduling, exactly-once guarantee hoặc general retry/recovery framework.
