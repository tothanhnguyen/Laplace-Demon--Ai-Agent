# Phase 2: Task card, ownership và vòng đời lượt xử lý

Status: Completed | Priority: P1 | Effort: 12h | Depends on: Phase 1

## Context và mục tiêu

- [Plan tổng](./plan.md) · [Context contract](./phase-01-context-builder.md) · [Memory/forget](./phase-03-compaction-session-memory.md).
- Schema thật: `Task(goal,status,user_id,conversation_id,...)`; `Step(step_index,name,status,detail,...)`. Không có `title`, `index`, `action_json`, `observation_json`.
- Vòng hiện tại có 5 tool executions nhưng tối đa 6 decision slots; schema retry không phải tool step.
- Deliverable: card có mục tiêu, tiêu chí hoàn thành, bước hiện tại, công cụ đã dùng và việc còn lại; mọi tool execution gắn đúng user/task; kết thúc/hủy không để worker bị nhầm là đã xong.

## Contract dữ liệu

### T1 — Task và Step

- Tool-less turn: có provisional card trong bộ nhớ nếu cần biểu diễn request; không tạo Task/Step trong DB. Layer 2 có thể chỉ chứa rolling summary.
- Trước tool đầu tiên, tạo đúng một Task: `user_id`, `conversation_id`, `goal=request` đầy đủ, `status='running'`.
- Thêm `Task.request_message_id: int | None`, FK `messages.id`, để gắn task vào đúng turn cho compaction và recall tool ở lượt sau. Legacy rows giữ NULL; không backfill bằng text/timestamp. Không thêm cột title hay các cột JSON không cần thiết.
- `Step.step_index`: decision slot 0-based. `Step.name`: tên tool hoặc event terminal. `Step.status`: `completed | failed | step_limit | cancelled` theo event; không dùng status task để giả định tool thành công.
- `Step.detail` JSON version 1: `{version, action, observation, task_state, stop_reason}`. `action` là `AgentAction` hợp lệ hoặc null cho schema_failure; `observation` chỉ `{tool_call_id, ok, source, preview}`, không copy full payload; `task_state` chứa criteria/remaining snapshot.
- Sau task đầu tiên, ghi một Step cho mỗi outer decision được xử lý, gồm terminal. Schema retries chỉ là `llm_calls`, không tăng step/tool count. Exception/cancel sau tool ghi terminal event ở decision slot tiếp theo; không tạo hai rows trùng event cho cùng lần kết thúc.
- Mọi ToolCall mới bắt buộc được gọi với `task_id`; Trace của tool/terminal có cả task_id và conversation_id, tool event thêm `tool_call_id`. LLMCall luôn có `user_id`; thêm task_id khi đã có task, không cần gán ngược call trước tool đầu tiên.

### T2 — Criteria/remaining có ý nghĩa, không giả làm verifier

Mở rộng `AgentAction` bằng **một field tùy chọn** `task_update`:

- `acceptance_criteria: list[str] | None`, `remaining_work: list[str] | None`.
- Mỗi list tối đa 5 mục, mỗi mục tối đa 200 ký tự; Pydantic validate. `None` giữ snapshot trước, `[]` là danh sách rỗng có chủ ý. Không thay meaning `action/tool/params/final_answer` hay số schema retries.
- System prompt yêu cầu mô hình báo criteria cụ thể theo request và remaining sau mỗi decision; không thêm lời gọi planning riêng. Đây là **kế hoạch do mô hình báo**, không phải bằng chứng đã thực hiện.
- Khi metadata chưa có: criteria = “Đáp ứng yêu cầu: <request preview>”; remaining = “Chưa được mô hình xác định”. Không gán mặc định “đã xong” chỉ vì tool chạy OK.
- Khi terminal final: task `completed`, card ghi “Mô hình đã trả lời cuối; chưa có lớp kiểm chứng độc lập”. Giữ remaining còn mở nếu model tự báo còn việc; không silently xóa để làm đẹp kết quả.
- Số tools dùng và ok/error lấy từ DB; không tin count do model báo. Goal lấy request gốc, không cho `task_update` ghi đè.

### T3 — Trạng thái và giới hạn

| Đường kết thúc | Task status | Step/trace stop_reason | Ghi chú |
|---|---|---|---|
| Model final sau tool | `completed` | `final` | Không đồng nghĩa verified |
| Hết schema retries sau tool | `failed` | `schema_failure` | Không thực thi tool từ JSON invalid |
| Đã chạy 5 tools, decision tiếp theo vẫn đòi tool | `step_limit` | `step_limit` | Không chạy tool thứ 6 |
| Tool trả lỗi | Vẫn `running` | observation `ok=false` | Cho model phản ứng; chỉ đóng khi có terminal |
| Provider/exception thường sau tool | `failed` | `provider_error` / `execution_error` | Ghi ở transaction mới; không đổi thành cancelled |
| Context bắt buộc quá lớn | `failed` | `context_overflow` | Dừng trước call tiếp theo |
| Worker nhận yêu cầu hủy tại checkpoint | `cancelled` | `cancelled` | Không đồng nhất với coroutine bị cancel |

Immediate final/schema failure/overflow trước tool: lưu message/trace phù hợp, không tạo task giả. Database outage hoặc process bị kill không thể bảo đảm ghi terminal; giữ limitation rõ, phục hồi crash thuộc Sprint 6.

### T4 — Vòng đời service và transaction

Thêm `laplace/services/memory.py` làm biên thao tác bộ nhớ/điều phối theo user, **không** chứa thuật toán context. Ban đầu cung cấp guard và cancellation; Phase 3 bổ sung view/forget.

- Registry nhỏ dùng stdlib lock bảo vệ acquire/release nguyên tử; entry theo `telegram_user_id` gồm busy owner và cancellation Event. Service cung cấp `try_reserve_turn(user_id)` trả lease có owner token hoặc busy; acquire không chờ. Entry đang dùng không bị eviction; bỏ entry idle sau khi không còn owner.
- Worker và forget cùng acquire guard. Nếu busy, trả trạng thái “đang xử lý”; không check-then-delete không khóa. Hai calls service trực tiếp cùng user cũng không chạy đồng thời.
- `/cancel` đặt Event, trả “Đã yêu cầu hủy, đang chờ bước hiện tại kết thúc”; **không** `task.cancel()` worker. Check trước mỗi provider attempt/tool và sau khi chúng trả về. Provider HTTP đang chạy không bị ngắt cưỡng bức; usage/result đã phát sinh vẫn được ghi.
- Bot gọi `try_reserve_turn` **trước await Telegram đầu tiên**; truyền lease tới helper nội bộ `chat._run_reserved_message(lease, ...)` chạy shared `_handle_message`. Public `handle_message` giữ chữ ký cũ, tự reserve rồi gọi cùng helper cho CLI/service callers. Helper kiểm lease đúng user/owner, không acquire lần hai; release chỉ trong finally của worker thật. Setup bot lỗi trước khi khởi chạy worker phải release lease; sau khi đã giao worker thì waiter không có quyền release.
- Bot giữ worker future thật, await qua shield nếu waiter có thể bị hủy; dọn `_running_tasks` và ngừng progress bằng completion của worker, không bằng completion của waiter. Forget dùng cùng registry nên cả request đã reserve nhưng worker chưa start cũng là busy; không có khe xóa thành công rồi một request đã nhận trước đó mới chạy ghi lại dữ liệu.
- Một process bot dùng DB; CLI dùng user 0. Guard không hỗ trợ nhiều processes đồng thời ghi cùng user; ghi rõ giới hạn triển khai, không quảng cáo distributed lock.
- Không giữ write transaction xuyên provider/tool call: transaction ngắn tạo request/identity; transaction tạo task trước tool; transaction ghi từng LLM result; transaction ghi tool+Step+Trace nguyên tử; transaction ghi final message + close task. Repo vẫn nhận `Session` đầu tiên, flush, không commit; service sở hữu `session_scope`.
- ORM objects không được đưa qua thread/session; giữ IDs/plain snapshots. Lỗi provider sau tool đã commit không rollback toàn lịch sử lượt; finalization dùng session mới. Lỗi DB không được catch rồi tiếp tục dùng session đã rollback hỏng.
- Worker canceled sau khi provider/tool trả về: ghi usage/result thật trước, rồi ghi cancelled; không thực thi bước kế tiếp. Nếu final đã commit trước khi nhận yêu cầu hủy, kết quả `completed` thắng; không đổi ngược terminal.

### T5 — Rendering và callback

`context.render_task_card(snapshot) -> str` là pure function. Snapshot đọc từ Task + Steps + ToolCalls, gồm goal preview, criteria, decision index, `tools_executed/5`, tên tools và số lần, remaining, task status/stop reason.

- Cập nhật sau mỗi Step đã commit, trước provider call kế tiếp; không query lại toàn lịch sử user ở từng bước.
- Progress callback nhận cùng snapshot đã render, cap 1.000 chars theo Phase 1; cắt từng field với marker, không cắt mất status. `_StatusEditor` vẫn coalesce, **không yêu cầu mỗi bước cực nhanh đều render thành một Telegram edit**.
- Tổng message tiến độ <=4.096 ký tự; cùng callback dùng được với CLI `print`. Không coi callback/Telegram edit lỗi là task thất bại; đóng editor sau terminal để progress cũ không ghi đè final.

## File map (từ `Laplace-Demon/`)

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/models.py`, `laplace/db.py` | Nullable `Task.request_message_id`; helper migration additive/idempotent dùng lại Phase 3 |
| Modify | `laplace/repo.py` | Session-first create_task/append_step/close_task/task_snapshot, ownership links |
| Modify | `laplace/prompts.py` | Optional task_update + hướng dẫn metadata |
| Modify | `laplace/context.py` | Render bounded card từ snapshot |
| Create | `laplace/services/memory.py` | Per-user reservation lease + cancellation lifecycle, acquire dùng chung với forget |
| Modify | `laplace/services/chat.py` | Public wrapper giữ signature; reserved-worker helper vào shared runtime; transactions, lifecycle, callback |
| Modify | `laplace/bot/handlers.py` | Admission reservation, cooperative /cancel, worker cleanup, editor close |
| Modify | `tests/test_chat_service.py`, `tests/test_db.py` | Terminal/ownership/upgrade regressions |
| Create | `tests/test_session_memory.py` | Guard/worker races; Phase 3 mở rộng cùng file |

## Đầu việc

| ID | Việc | Effort | Phụ thuộc/kết quả |
|---|---|---|---|
| S3-05 | Mapping schema, request FK, task_update và snapshot | 3h | T1/T2 cố định; nâng cấp không mất rows cũ |
| S3-06 | Ownership + Steps + terminal transitions + transactions ngắn | 4h | T3, full tool result luôn truy về user qua task |
| S3-07 | Guard, cooperative cancel và worker lifecycle bot | 3h | T4; forget có thể reuse guard, không còn cancel-waiter-as-worker |
| S3-08 | Card integration + smoke và regression quan trọng | 2h | T5 + G2 dưới đây |

## Nghiệm thu G2

- **T-01:** Tool→final, tool→schema_failure, tool-error→final, 5 tools→tool thứ 6 bị chặn: đúng terminal và đúng số lần tool thực sự chạy; decision 6 không hiển thị như 6/5 tools.
- **T-02:** Hai requests trùng text tạo task liên kết đúng request ID; mọi ToolCall/Trace truy về đúng user; chat thuần không có Task/Step.
- **T-03:** Provider lỗi sau một tool: tool result/usage trước lỗi còn trong DB; task failed và stop_reason cụ thể. Không test “mark rồi re-raise trong transaction” như thể đã commit.
- **T-04:** Dùng `threading.Event` chặn provider thật trong worker test, gọi cancel: busy còn cho tới worker exit, không có tool tiếp; usage call đang chạy không mất. Không dùng sleep làm đồng bộ.
- **T-05:** Hai chat same-user cùng lúc chỉ một vào runtime; user khác không dùng chung guard. Test dùng SQLite file tạm nhiều connections, không giả lập concurrency bằng một in-memory connection.
- **T-06:** Card criteria/remaining cập nhật khi metadata đổi, count dựa tool DB; provider call kế tiếp thấy card mới; callback lỗi không thay terminal.
- **T-07:** DB Sprint 2 có dữ liệu nâng cấp task FK được; init hai lần không mất/copy rows. Giữ legacy task.request_message_id NULL và hiển thị đúng giới hạn liên kết.

Smoke CLI/tool turn bằng DB tạm + fixture file; lưu card callback và task/steps/tool rows thực. Telegram smoke sẽ gộp ở G3 để không bắt mỗi checkpoint dùng API thật. Tests chỉ bảo vệ T-01 đến T-07 với consumer-visible behavior, không tạo file riêng chỉ kiểm wording card.

## Checklist, rủi ro và bàn giao

- [x] S3-05 đến S3-08 hoàn tất; bằng chứng G2 nằm trong regression task/terminal/race.
- [x] Không task mới thiếu request ownership hoặc ToolCall mới thiếu task_id.
- [x] Không network call trong transaction giữ write lock; repo không tự commit.
- [x] `/cancel` không nói đã hủy khi worker còn chạy; task terminal chỉ được ghi một lần.
- [x] Phase 3 phụ thuộc Phase 2.

Rủi ro: đổi transaction có thể mất attribution/cost nếu chỉ ghi cuối lượt; T-03/T-04 chặn. Guard process-local không phục hồi crash; không thêm job queue, background resume hay distributed infrastructure vào scope.
