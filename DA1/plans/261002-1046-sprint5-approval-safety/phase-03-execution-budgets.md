---
title: "Phase 3: Ngân sách thực thi và dừng lỗi lặp"
status: pending
priority: P0
effort: "12h"
dependsOn: [phase-02-telegram-approval]
---

# Phase 3: Ngân sách thực thi và dừng lỗi lặp

## Context và vấn đề

[Plan tổng](./plan.md) · [Approval checkpoint](./phase-02-telegram-approval.md). S5-05; không xây retry/recovery framework Sprint 6.

Baseline đã đối chiếu:

- `services/chat.py:39-40,507-554,689-700,751-806,1048-1108`: 5 tool executions, schema retry tối đa 2; compaction trước task đầu tiên, có decision/final-only calls ngoài tool counter.
- `llm/openai_provider.py:86,110-142,156-169`: chưa explicit SDK timeout/retry/output cap; thêm schema sau context builder; vòng SDK calls/backoff riêng; usage thiếu hiện thành 0, model không có giá có thể trả cost 0.
- `scripts/eval_context.py` có observed-cost limiter **chỉ cho evaluator**, không reserve request kế, không dùng nguyên wrapper làm runtime budget.

Decision: một `TurnBudget` từ **trước compaction/first LLM call**, truyền qua provider physical attempts và gateway; checkpoint giữ counters khi paused. Budget theo nhiệm vụ/lượt, kể cả direct-final không tạo tool Task. Không tạo Task cho chat thuần chỉ để tính budget; usage vẫn owner/request-linked.

## Config và định nghĩa

| Setting mới | Default | Miền hợp lệ / ý nghĩa |
|---|---:|---|
| `LAPLACE_MAX_STEPS` | 5 | Integer 1–20; số tool proposals/attempts, kể cả invalid/denied. |
| `LAPLACE_MAX_SECONDS` | 120 | Finite 1–600; active execution time, không gồm chờ approval. |
| `LAPLACE_MAX_COST_PER_TASK` | 0.10 USD | Finite 0–10; budget theo **ước tính** giá, không phải trần hóa đơn. Zero chỉ cho known-zero model. |
| `LAPLACE_LLM_MAX_COMPLETION_TOKENS` | 2048 | Integer 1–8192; cap gửi SDK, không silently bỏ nếu backend reject. |
| `LAPLACE_LLM_TIMEOUT_SECONDS` | 30 | Finite 1–120; total physical-attempt deadline, clamp theo task remaining. |
| `LAPLACE_MAX_REPEATED_ERRORS` | 3 | Integer 1–5; `streak >= configured_limit` thì dừng, 3 là default. |

Không NaN/Inf, không negative hoặc vô hạn. Approval TTL wall-clock 900s riêng (config 1–900, mặc định 900). Notification dispatcher không mở lại task budget; nó giữ timeout/retry contract Sprint 4.

## Step, time và stop contract

- Count tool attempt khi gateway admit proposal; pending → approve execution là **cùng attempt**, không tính hai bước. Denied/invalid action được count để không spam policy; replay receipt không gọi tool nhưng vẫn bounded decision path, không cấp thêm quota.
- Không gọi tool N+1. Có thể final synthesis sau N nếu còn time/cost; khi hết time/cost dùng deterministic response + committed effect receipts, không gọi “một LLM cuối để giải thích”.
- `TurnBudget` đo monotonic active segments từ trước compaction; pause giữ elapsed rồi ngừng clock; resume không reset steps/spend/errors. Retry/backoff thuộc active time; UTC chỉ dùng cho approval/reminder timestamps.
- **Không nhầm I/O timeout với total deadline.** Giữ public `LLMProvider.complete` đồng bộ cho chat/CLI; adapter gọi một coroutine private bằng `asyncio.run` từ worker thread/CLI, dùng `AsyncOpenAI` + `asyncio.timeout(min(attempt_limit, task_remaining))` bao quanh physical attempt, await cleanup client. Retry sleep dùng `asyncio.sleep` trong remaining deadline. Không gọi sync SDK bên trong coroutine timeout.
- Web adapter áp cùng cách: sync interface hiện có bọc `httpx.AsyncClient` + total timeout; parser tiếp tục killable subprocess. Không `wait_for(to_thread(...))` rồi giải phóng lease giả. Các async bridges chỉ chạy tại sync worker/CLI, không gọi lồng trên bot event loop; fake adapters vẫn theo interface sync. Không tạo event loop/thread/service dùng chung mới.
- Check deadline/cancel trước và sau external I/O, trước write/approval consume; DB transaction ngắn dùng busy timeout hữu hạn ≤ remaining, không giữ qua network. Không hứa rollback write đã commit hoặc CPU/OS scheduling ngắt đúng millisecond: nếu return muộn thì ghi elapsed/stop, không request/effect mới. Lease release sau actual worker exit. Test slow-drip và backoff chứng minh network attempt bị hủy, không chỉ test delay trước call.
- Stop reasons explicit: `step_limit`, `time_limit`, `cost_limit`, `budget_unavailable`, `repeated_error`, cộng cancellation/approval states. Task card hiển thị used/remaining thực, bỏ `/5` hardcoded khi config khác.

## Chi phí: một công thức ước tính, một writer

**Chốt phạm vi:** giới hạn chi phí ứng dụng bằng reservation ước tính trước call + usage thực trả về sau call. Không yêu cầu chứng minh token upper bound cho mọi backend, không khóa toàn bộ paid presets chỉ vì thiếu tokenizer chính xác. Sai số/hidden provider billing có thể làm overshoot một attempt; ghi rõ, dừng ngay, không gọi là hard invoice cap.

### Reservation và provider compatibility

1. Dựng payload thực, gồm schema/system text adapter thêm. Với text-only chat hiện tại, dùng estimator bảo thủ thống nhất: `input_estimate = len(compact_payload_json.encode("utf-8")) + 256 + 32 * message_count`. Đây là **heuristic có version**, không phải định lý tokenizer/hidden reasoning bound.
2. Giá trong preset là **USD / 1.000.000 tokens**. Reserve `R = (input_estimate * input_price + completion_cap * output_price) / 1_000_000`; không làm tròn xuống trước so sánh. Dùng Decimal cho budget arithmetic; DB float hiện có là representation báo cáo, không lấy float rounding làm quyết định cap.
3. `charge + R > cap` → `cost_limit` trước SDK. Mỗi physical retry/format fallback có reservation riêng. Explicit SDK `max_retries=0`; không thêm loại retry mới, không bỏ output cap trong format fallback.
4. Response có usage + giá known: settle theo actual token estimate, release phần dư. Nếu actual vượt R/cap, ghi overshoot và dừng trước call/effect tiếp. Nếu usage thiếu/alias lạ/timeout mơ hồ: giữ R làm charge, status unknown và **dừng turn `budget_unavailable`**, không refund zero hoặc tiếp tục mutation dựa trên result không xác định budget.
5. 429/400 cũng giữ reservation conservative trừ khi có contract chứng minh không billable; retry hiện có được phép nếu còn budget/time, không tự coi errors là free. Missing known price trước call → fail-closed `budget_unavailable`, không fallback mock. `mock` chỉ khi được chọn explicit; typo provider → configuration error.
6. Known-zero router9 dùng R=0 nhưng vẫn giữ missing-usage/attempt status, step/time/output cap. Quota/subscription không được quy đổi thành USD. Compaction, schema repair, final-only, effect-recovery và resume đều qua cùng boundary.

| Adapter/profile | Output-cap parameter gửi SDK | Giá/alias | Cách nghiệm thu |
|---|---|---|---|
| `mock` explicit | Không network, fake adapter phải nhận call limits | Synthetic metrics, không proof giá thật | Deterministic boundaries. |
| OpenAI defaults `gpt-4o-mini` và existing non-reasoning presets | `max_tokens` | Giá exact trong preset; response snapshot aliases phải khai báo, không wildcard zero | Fake HTTP responses kiểm reserve/settle/overshoot; paid live chỉ khi có key/quota, report riêng. |
| Gemini/Groq existing defaults | `max_tokens` qua OpenAI-compatible endpoint | Giá exact theo preset | Contract fixture + optional live theo config thực. |
| B.AI `gpt-5.2`, router9 `cx/gpt-5.6-sol` | `max_completion_tokens` | B.AI có giá; router9 hai alias known-zero hiện có | router9 là live baseline đã dùng Sprint 4; kiểm backend thực chấp nhận cap trước G3 sign-off. |

Đây là mapping triển khai dự kiến, **không phải kết quả live đã xác minh**. Backend reject cap → configuration error; nếu cần đổi tên tham số, cập nhật profile dựa response/docs rồi chạy lại contract check, không retry request uncapped. Model override ngoài pricing/profile được báo unsupported trước network; không ngầm làm mất khả năng dùng preset hiện có. Không thêm dependency tokenizer hoặc framework pricing trong sprint này.

### Một row `LLMCall` = một physical attempt mới

`services/budget.py` cung cấp attempt recorder (nhận repo/session factory). Provider gọi recorder trước/sau từng SDK invocation; **recorder là writer duy nhất**. Chat `_record_llm_result`/`_TurnRecorder` chỉ tổng hợp result đã ghi, không insert một logical-success row nữa. Provider không tự import repo hoặc đọc DB; callback accounting được inject từ runtime.

| Additive field | Kiểu/giá trị | Quy tắc |
|---|---|---|
| `request_message_id` | Nullable FK Message | Calls trước Task vẫn gắn đúng request; forget set NULL trước xóa message. |
| `call_group_id`, `attempt_index` | Nullable string / integer | UUID cho một `complete`; unique pair cho attempts mới, legacy NULL. |
| `accounting_status` | Nullable string | NULL = legacy estimate; mới: reserved → settled hoặc unknown. |
| `budget_charge_usd` | Nullable float | Reserve trước send, actual sau settle, reserve giữ lại nếu unknown; không copy prompt/body. |
| `accounting_detail` | Nullable JSON text | Allowlist: requested/returned model, estimator/pricing version, rate pair, attempt outcome và token-usage-known flag. |

Reserve row commit trước send; DB write lỗi thì không gửi. Finalize lỗi sau send → giữ reserved row, stop turn; startup đổi reserved còn dở thành unknown, giữ charge, không replay. `cost_usd`/tokens cũ chỉ có ý nghĩa actual estimate khi settled hoặc legacy; unknown có `cost_usd=0` storage mặc định nhưng `/status` **không được trình bày là miễn phí**. Aggregate trả riêng `known_cost_usd`, `budget_charge_usd`, `unknown_attempts`, `physical_attempts`; group count tính logical calls, không đổi nghĩa metric cũ âm thầm. CLI/bot/evaluator/renderers cùng migrate.

Khi Task được tạo, attach calls theo `(user_id, request_message_id)`, không theo “latest calls”. `/forget` giữ rows + owner/accounting nhưng detach task/request FKs. Task card lấy cumulative budget từ `TurnState`; không cần bảng ledger hoặc persist checkpoint để restart resume.

## Lỗi lặp

Fingerprint tool: `(tool_name, stable error_code)`, không raw error/params/PII. Cùng fingerprint liên tiếp đến `LAPLACE_MAX_REPEATED_ERRORS` (default 3) → `repeated_error`, không tool/LLM attempt tiếp; success hoặc fingerprint khác reset streak. Schema correction giữ tối đa 2 retry riêng; physical provider retry được bound bằng attempts/time/cost, không trộn vào tool streak.

Checkpoint lưu fingerprint/count; resume không reset. Permission denial không biến thành approval hoặc retry loop. Không tăng MAX_STEPS để “thử thêm lần cuối”.

## File map và bước triển khai

| Action | File | Thay đổi |
|---|---|---|
| Create | `laplace/services/budget.py` | Small TurnBudget value/state, reserve/reconcile, active segments, failure streak và stop reason. |
| Modify | `laplace/config.py`, `.env.example`, `README.md` | Validated settings, pricing/time/known-zero/unknown disclosure. |
| Modify | `laplace/llm/base.py`, `laplace/llm/openai_provider.py`, `laplace/llm/mock.py`, `laplace/llm/presets.py` | Call options/attempt observer, timeout/output cap, explicit retry ownership, pricing provenance. |
| Modify | `laplace/services/chat.py`, `laplace/services/approvals.py`, `laplace/tools/base.py`, `laplace/context.py` | Budget trước mọi call/write; preserve qua pause; correct card và deterministic stop. |
| Modify | `laplace/services/web.py`, `laplace/services/documents.py`, `laplace/tools/web.py`, `laplace/tools/documents.py` | Clamp existing tool I/O/parse deadline; không thêm retry framework. |
| Modify | `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` | Additive attempt/accounting metadata, request attribution, owner usage projections. |
| Modify | `scripts/eval_context.py`, `tests/test_eval_context.py` | Migrate provider protocol/callers, giữ evaluator intent; không thay bằng runtime thứ hai. |
| Modify | `tests/test_chat_service.py`, `tests/test_session_memory.py`, `tests/test_db.py`, `tests/test_context.py`, `tests/test_approvals.py` | Budgets/resume/accounting invariants, migration và existing cancellation. |
| Create | `tests/test_execution_budget.py` | Boundary/error/unknown pricing/physical attempt tests với fake clock + provider transport. |

1. LSP references trước đổi exported `LLMProvider.complete`/`LLMResult`; migrate MockLLM, presets, evaluator wrappers và callers trong một cutover, không fallback signature cũ.
2. Add validated budget + minimal accounting migration; init hai lần không đổi historical totals/data.
3. Wire trước compaction/agent/final, provider attempt và tool admission; không chỉ wrapper ngoài `handle_message`.
4. Reuse existing tests cho usage/watermark/cancel; thêm tests cho uncertain budget boundaries. Smoke shared runtime với controlled slow/failed provider adapter + actual DB, quan sát stop và zero subsequent effects.

## Thứ tự code

| ID | Việc cần làm | Đầu ra |
|---|---|---|
| P3-01 | Validated config + `TurnBudget`, clock/steps/streak/reservation. | Boundary và non-default settings deterministic. |
| P3-02 | Additive attempt fields/index + recorder writer duy nhất, update aggregates/forget. | Reserve/settle/unknown không duplicate rows; usage cũ nguyên vẹn. |
| P3-03 | Migrate provider protocol call limits/recorder, async SDK bridge + explicit retries/caps/profiles. | Mỗi physical attempt có row và total timeout; không uncapped fallback. |
| P3-04 | Wire budget trước compaction, agent/schema/final/tool; preserve checkpoint, clamp web/parser/DB deadline. | Mọi path dùng cùng counters; approved tool không tính hai steps. |
| P3-05 | Migrate CLI/bot/eval consumers và card; smoke + gate G3. | Known/unknown accounting rõ; slow-drip dừng; receipt không mất khi hết budget. |

## Checklist và gate G3

- [ ] N/N+1 step, exact deadline/cost boundary và final-only path đều đúng.
- [ ] Physical attempts được reserve/account một lần; total async timeout kiểm bằng slow-drip; không hidden SDK retry.
- [ ] Paid fixture reserve/settle hoạt động với đúng đơn vị giá; known-zero khác unknown; profile/cap được live-check trên provider baseline.
- [ ] Compaction trước task, repair và resume cùng budget; no extra LLM khi budget terminal.
- [ ] Lỗi thứ 3 terminal ở default; một cấu hình khác cũng đúng boundary, fingerprint/success reset đúng, resume không reset.
- [ ] `/status`, task card và forget giữ accounting trung thực, không xóa usage.

## Rủi ro và handoff

P0: overshoot estimate không thể hoàn tiền request đã gửi → ghi observed charge và dừng, không hứa hard invoice cap. P0: I/O timeout không phải total deadline → async transport thật dưới timeout, không thread giả cancellation. Phase 4 bảo vệ log accounting/errors; Phase 5 kiểm DB/receipts thay vì wording.
