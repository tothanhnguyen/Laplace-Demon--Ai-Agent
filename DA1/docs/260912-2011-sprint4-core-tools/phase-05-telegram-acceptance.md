---
title: "Phase 5: Telegram integration and midterm acceptance"
status: completed
priority: P0
effort: "10h"
dependsOn: [phase-02-web-research, phase-03-document-processing, phase-04-reminder-scheduler]
---

# Phase 5: Telegram integration, hardening và nghiệm thu

Status: Completed — owner-confirmed live Telegram acceptance on 2026-10-02 | Priority: P0 | Effort: 10h | Depends on: Phase 2–4

Automated acceptance 2026-09-21: `148 passed` trên Python 3.14 và Python 3.11,
Ruff và `git diff --check` đều sạch. Focused Sprint 4 suite pass `95/95`;
shared production turn đã quan sát fake Tavily
`web_search → fetch_page → create_reminder → final`, URL sources bền trong DB,
reminder chỉ được cấp quyền bởi directive rõ ràng và tool data/câu văn mô tả
không tự cấp capability.
Smoke không-test dùng DB tạm + actual dispatcher quan sát `pending → delivering → sent`, một attempt, Telegram message ID `4242` và sent epoch. Các live acceptance gates đã được chủ dự án xác nhận hoàn tất; chi tiết và giới hạn bằng chứng được ghi dưới đây.

### Live evidence update — 2026-09-29

- The user provided DOCX and TXT responses from the Telegram retest. DOCX returned five findings with paragraph/table locators; TXT returned five findings plus a synthesis with line locators. The shown responses retained goals, owners, deadlines, and risks.
- A user-provided Telegram screenshot shows reminder `#4` created pending at `2026-09-29T01:59:26Z` (08:59:26 Vietnam time) after the 08:54 request, then a reminder notification at 08:59. This verifies live create-and-deliver for one reminder only; restart recovery, update/cancel, and exact delivery SLA remain open.
- The failed Telegram weather turn had one final `agent_action` LLM call and no task/tool-call row; the model returned a no-results claim without invoking `web_search`. A direct Tavily check then returned results and extracted a public page, isolating the failure to model tool selection rather than Tavily configuration/network.
- Runtime now forces `web_search` for explicit web-search requests and `fetch_page` when the user also explicitly asks to read a source. The real shared chat runtime smoke returned `web_search → fetch_page`, both successful with the same source URL, and a Vietnamese weather summary.
- Focused chat/web tests: `63 passed`; full Python suite after the fix: `164 passed`; Ruff: passed.
- An initial bot restart attempt did not reach confirmed readiness; direct Telegram `getMe` timed out after 45 seconds, so that process was stopped.
- Subsequent live Telegram run (task `14`) stored successful `web_search` (14,501 ms) and `fetch_page` (1,811 ms) calls. The extracted URL matches the source shown in the user's screenshot; the response included a weather summary and the source preview. This passes one L2 web case, not the combined reminder/restart flow.
- The owner confirms all remaining acceptance gates passed: L1 preflight; live missing-key/corrupt/oversize checks; reminder recovery within misfire grace and crash-after-claim; and `/forget`. Raw per-run IDs/timestamps are not present in this workspace.

### Live evidence update — 2026-10-01

- Manual locator cross-check against the bundled TXT/DOCX acceptance fixtures passed: TXT citations point to lines 4–9 for objective, decision, owners, dates and risk; DOCX citations point to paragraphs 2–4 and `table:1/row:2–4`, matching the source content.
- Synthetic fixture manifest: PDF (1 page, PDF 1.3) SHA-256 `3b3f693ade140428052dfb7cdf219b61adde80f12c53126a5a890df2c8d63435`; DOCX SHA-256 `dadef2e55df822dd9685792ba7b5ff7457c82dacc496506de65b3fe67085f90b`; UTF-8 TXT SHA-256 `aa1ddf062987c062999818fa4918c7518eb4bf97f61e4148705982ab16e5a5e5`.
- The owner confirms a later corrected Telegram E2E completed web search, source reading, reminder creation, process restart against the same DB, and post-restart delivery. This is owner-attested; the run-specific reminder ID, timestamps and Telegram message ID were not added to this workspace.
- Keep earlier local evidence distinct: task `16` stored `create_reminder → web_search` without `fetch_page`, and reminder `#5` was delivered by the same process that created it (`sent`, one attempt, Telegram message ID `107`). Those records do not prove the later restart run.

### Owner confirmation — 2026-10-02

- The owner confirms every required Telegram acceptance case passed, including web/source reading, document locators, reminder create/update/cancel, pre-due restart and post-restart delivery, misfire-grace and crash-after-claim recovery, `/forget`, missing Tavily key, corrupt/unsupported/oversize uploads, and honest scan-only PDF handling. This is owner-attested; raw per-run IDs/timestamps and Telegram message IDs were not added to the workspace.


## Mục tiêu

Chứng minh ba tool groups chạy trên cùng production path của Telegram, không chỉ unit test: web có URL evidence, document có locator, reminder persist/restart/send. Chốt migration, lifecycle, privacy text, docs và video/report giữa kỳ dựa trên behavior đã quan sát.

- [Plan tổng](./plan.md) · [Web](./phase-02-web-research.md) · [Document](./phase-03-document-processing.md) · [Reminder](./phase-04-reminder-scheduler.md)
- Entry point thật: `.venv/bin/python -m laplace --bot` (`README.md:84-92`).
- Baseline handler chạy shared sync chat trong `asyncio.to_thread` và giữ worker lease `handlers.py:274-330`; integration phải reuse, không tạo agent loop thứ hai cho documents.

## Integration flow

### I1 — Shared Telegram turn runner

Refactor phần text handler thành helper async dùng chung:

1. Reserve `TurnLease` trước await đầu tiên có thể nhận thêm request.
2. Gửi typing/status, tạo `_StatusEditor`.
3. Download/validate document nếu có; tạo `AttachmentRef` scoped.
4. Chạy `_run_reserved_message` trong worker với trusted transport facts + attachments.
5. Shield worker waiter, track `_running_tasks`, split final response, release chỉ trong worker ownership/finally đúng contract Sprint 3.

Text behavior/public `handle_message` compatibility giữ nguyên. Document caption là request; caption rỗng dùng default summary request. Setup/download fail trước worker phải release lease và cleanup; sau worker start, waiter không release hộ.

### I2 — Bot process lifecycle

`run_bot()` order cuối:

```text
settings/token → init_db → Bot/Dispatcher/router → reminder recovery/start
→ start_polling → reminder stop/await → Bot session close
```

- Scheduler nhận chính Bot instance đang live.
- Polling exception vẫn chạy same cleanup order.
- Web HTTP client/tool không giữ global session vượt lifecycle; sync requests chạy worker thread.
- Không startup message, reminder hoặc live web call tự động ngoài user data.

### I3 — User-visible commands/disclosures

Update `/start`/`/help`:

- Supported web/document examples và reminder directives `/remind`,
  `/remind_update`, `/remind_cancel`.
- Documents/reminders chỉ private chat; supported types/10 MiB/no OCR.
- How to refer to reminder ID from create response or `/memory`.

Update `/memory`/`/forget`:

- Pending reminder previews owner-scoped.
- Forget warning/result explicitly deletes reminders and keeps usage/provider/Telegram
  history; upload tạm đã bị xóa sau document turn và không thuộc memory.
- Busy text covers chat, document processing, reminder delivery and forget; `/cancel` still cancels only chat/document agent turn, not in-flight reminder delivery.

## Automated acceptance matrix

Focused suites trước, full suite một lần cuối:

| Gate | Scenario | Observable contract |
|---|---|---|
| A1 Registry | All production built-ins loaded | Unique strict specs; `read_file` absent; forged context/path impossible; canonical JSON/source/latency |
| A2 Web | Fake Tavily search→extract→final | Two owned calls, URLs survive DB/context; 200 full/partial failure modeled; no unsupported citation |
| A3 Document | Synthetic PDF/DOCX/TXT upload→summary | Caption/default same-turn route, parser resource isolation, locator-backed full/partial result |
| A4 Reminder | Chat create/update/cancel | DB state follows owner/time contract; no model-chosen recipient |
| A5 Restart/retry | Same temp DB, scheduler/process recreated | Future/due/stale-claim rows recover; persistent attempt budget applied; duplicate risk explicit |
| A6 Failure | Key absent, HTTP fail, file/parser fail, Telegram 429/timeout | Structured honest failure/retry; task/runtime remains inspectable |
| A7 Privacy | Two users + forget/delivery race | Lease-before-claim; no cross-read/update/send; pre-confirm disclosure; usage/uploads accurate |
| A8 Migration | Sprint 3 DB snapshot, init twice | Reminder retry/timing columns/indexes present; prior rows unchanged; FK check clean |
| A9 Context | Large web/doc result + five-step boundary | Source metadata retained; four document reads reserve final; full/partial coverage truthful |
| A10 Lifecycle/SLA | Polling normal/failure + fake clock | Scheduler stops before Bot close; no orphan; unloaded first-attempt ≤2s/success ≤12s |

Run order:

```bash
.venv/bin/python -m pytest tests/test_tool_registry.py tests/test_web_tools.py tests/test_document_tools.py tests/test_reminders.py tests/test_bot_runner.py
.venv/bin/python -m pytest tests/test_chat_service.py tests/test_context.py tests/test_session_memory.py tests/test_bot_handlers.py tests/test_db.py
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Không dùng số lượng tests làm acceptance. Mỗi regression phải fail trên plausible bug: cross-owner/path access, wrong state transition, lost restart row, duplicate live claim, unrecovered stale claim, false citation, parser resource escape, context overflow, partial-file leak hoặc lifecycle inversion.

## Live Telegram acceptance

### L1 — Preflight

- Fresh private acceptance environment; backup existing `laplace.db` and `var/uploads` rather than deleting user data.
- `LAPLACE_TELEGRAM_BOT_TOKEN`, one real LLM key/provider, `LAPLACE_TAVILY_API_KEY`, `LAPLACE_TIMEZONE=Asia/Ho_Chi_Minh` loaded outside git.
- Synthetic/public PDF, DOCX, TXT and one scan-only PDF; no personal document.
- Record commit hash/branch, UTC/local timestamp, dependency versions and sanitized configuration flags; never record secret values.

### L2 — Three tool groups

1. **Web:** ask a current factual question; require search + extract; verify every final URL occurs in stored sources and opens to supporting content.
2. **PDF/DOCX/TXT:** upload each sample with a concrete summary/key-point request; verify cited page/paragraph/line against source. Scan PDF must say OCR unsupported.
3. **Reminder:** create a reminder 3–5 minutes ahead; capture returned ID/time; update message/time; create a second then cancel it; confirm cancelled one never fires.

### L3 — Required multi-step E2E + restart

Use one direct user request equivalent to:

> “Tìm thông tin mới nhất về X, đọc nguồn phù hợp, tóm tắt kèm link; /remind xem lại kết quả sau 5 phút.”

Expected production trace:

```text
web_search → fetch_page → create_reminder → final confirmation
```

Then:

1. Stop bot cleanly after reminder persisted but before due.
2. Start bot again against the same SQLite DB.
3. Confirm reminder remains pending with same ID/time.
4. Observe notification to the same private user; DB becomes `sent` with actual Telegram message ID. Với một reminder unloaded: `first_attempt_at_utc - due_at_utc <= 2s`, Telegram success ≤12s sau due.
5. Run `/memory`, then inspect `/forget` pre-confirm warning in a disposable acceptance account; confirm only after warning explicitly names reminder deletion. Verify reminders removed, usage unchanged, upload-retention disclosure accurate.

Also run downtime-crosses-due within 3.600s grace; notification must be marked late. Automated process-death-after-claim must requeue within max 3 persistent attempts; because contract is at-least-once, evidence must acknowledge a duplicate is possible when Telegram accepted before the crash.

### L4 — Failure live checks

- In a separate process/environment, unset Tavily key: web call reports configuration failure, no invented answer.
- Upload corrupt/unsupported file: no success summary and no invalid retained artifact.
- Fake Telegram adapter covers 429 `retry_after`, 5xx, timeout, permanent reject and process death after claim; do not block a real user just for failure testing.
- Real forced crash ambiguity is not used as “exactly once” evidence. Automated acceptance proves persistent requeue/attempt cap; report states crash-after-accept can duplicate the stable reminder ID.

## Midterm evidence package

Public project deliverables may include a sanitized Sprint 4 report/video checklist, but must never mention this private plan path.

Required evidence:

- Requirement matrix S4-01…S4-06 → code path → automated gate → live observation.
- Registry spec excerpt without secrets; DB schema/index/state snapshots with synthetic data.
- Web request IDs/URLs/latency and manual source check.
- Document sample checksums/types/locators, not raw user files.
- Reminder create/update/cancel/restart timestamps and Telegram message ID.
- Honest limitations: no OCR, 4.000-char full-summary budget, bounded partial docs, no Approval Gate yet, single process, at-least-once reminder duplicates at crash window.
- Web deviation record: hosted Tavily Extract, no fake missing-key stub, cache deferred; include owner/supervisor review status before claiming S4-02 complete.
- Video flow: architecture 1 phút; web 2 phút; documents 3 phút; reminder update/cancel/restart 3 phút; failure/limitations 1 phút.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/bot/handlers.py` | Shared turn runner, document entry, help/memory/forget text |
| Modify | `laplace/bot/runner.py` | Scheduler lifecycle ordering |
| Modify | `laplace/services/chat.py` | Final transport/attachment/runtime-facts integration |
| Modify | `README.md`, `.env.example` | Setup, examples, limits, operations and privacy |
| Modify/Create | `docs/` public Sprint 4 guide/report as project convention requires | Only observed behavior and sanitized evidence |
| Modify | `.gitignore` only if generated/private runtime artifacts are not already excluded | Prevent DB/uploads/evidence secrets from commit |

## Đầu việc

| ID | Việc | Effort | Gate |
|---|---|---:|---|
| S4-06A | Merge shared Telegram flow + scheduler lifecycle | 3h | No duplicate runtime, cleanup ordered |
| S4-06B | Run cross-tool/migration/privacy focused checks | 2h | A1–A10 |
| S4-06C | Run live web/document/reminder/restart scenarios | 3h | L1–L4 evidence |
| S4-06D | Full gates, docs/report/video checklist, cleanup | 2h | Definition of Done |

## Exit checklist

- [x] Three tool groups exercised through real Telegram, not direct unit calls only.
- [x] Multi-step flow, restart notification, reminder lifecycle, failure/recovery gates and `/forget` passed per owner confirmation on 2026-10-02; run-specific identifiers/timestamps were not retained in this workspace.
- [x] Failure paths do not return fake success or leak secrets/local paths.
- [x] No public file references `/Users/thanhnguyen/Documents/DA1/docs/...` or private plan slug.
- [x] Generated temp DB/uploads/API payloads removed or gitignored; no user data committed.
- [x] Sanitized public Sprint 4 acceptance report and video checklist distinguish automated, smoke and live evidence: [`docs/reports/261002-bao-cao-sprint-04.md`](../reports/261002-bao-cao-sprint-04.md).
- [x] Full pytest and ruff pass after focused scenarios.

Rollback: stop bot/scheduler first, restore backed-up DB/uploads and previous dependency environment; do not drop tables or delete active user data as a rollback shortcut.
