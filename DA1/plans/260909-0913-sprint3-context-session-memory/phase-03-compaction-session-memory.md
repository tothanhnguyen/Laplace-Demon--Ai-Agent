# Phase 3: Compaction và lệnh xem/xóa bộ nhớ phiên

Status: Completed — M-14 passed | Priority: P1 | Effort: 16h | Depends on: Phase 1 **và Phase 2**

## Context và yêu cầu

- [Plan tổng](./plan.md) · [Context/budget](./phase-01-context-builder.md) · [Ownership/guard](./phase-02-task-card.md).
- DB hiện chỉ persist user/final assistant messages; tool observations không nằm trong history. Muốn tóm tắt tool phải đọc ToolCall có task ownership, không tìm chuỗi `OK` trong messages.
- `init_db` hiện chỉ `create_all`; không tự ALTER bảng cũ. Không có cascade delete hoặc FK pragma trong engine hiện tại.
- Deliverable: hội thoại nhiều lượt dùng rolling memory có giới hạn; người dùng xem được nội dung phiên thực tế và xóa được dữ liệu có ownership mà không mất usage hoặc ảnh hưởng người khác.

## M1 — Schema và migration

Thêm vào Conversation:

| Field | Kiểu/default | Ý nghĩa |
|---|---|---|
| `summary` | Text nullable / NULL | JSON summary version 1 (plain data, không executable prompt) |
| `summary_until_message_id` | Integer nullable / NULL | Cutoff inclusive trong **chính conversation này**; NULL nghĩa chưa compact |

- `summary_until_message_id` là watermark, không thêm FK ngược tạo chu kỳ dependency; mọi query kiểm tra conversation_id.
- Reuse helper migration Phase 2 trong `db.py`: inspect `PRAGMA table_info`, thêm riêng từng cột còn thiếu, rồi cho phép ORM query. Không dựa vào `create_all` để ALTER. Hỗ trợ DB fresh, Sprint 2, partial upgrade và chạy lại.
- Engine bật `PRAGMA foreign_keys=ON` cho **mọi connection**, cả file/in-memory. Trước bàn giao chạy `foreign_key_check` trên DB fixture nâng cấp; nếu DB thật có dangling FK, không tự sửa/xóa rows không rõ chủ sở hữu.
- Backup SQLite nhất quán khi app dừng hoặc qua SQLite backup API trước migration. Không `cp` một DB đang ghi rồi coi đó là backup đủ. Rollback: revert code tương ứng và restore backup đã kiểm tra; không mặc định drop column/xóa `laplace.db`.
- Không thay đổi data người dùng thật trong lúc thực hiện migration smoke; dùng fixture DB Sprint 2 có rows và preserved usage.

## M2 — Summary contract và nguồn dữ liệu

Summary JSON version 1 gồm `entries` và `omitted_count`. Mỗi entry:
`{kind, text, source_message_ids, source_tool_call_ids}`; `kind` thuộc `decision | reason | result | source | user_fact`. Đây là lời ghi nhớ có nguồn, không là kết luận đã được verifier xác minh.

- Tối đa 10 entries; `text` <=250 chars; tổng JSON + wrapper dùng ở layer 2 <=2.000 chars theo C3. Khi giảm phải bỏ cả entry, không cắt JSON giữa chừng.
- Source IDs phải thuộc candidate batch hoặc previous summary của đúng user/conversation; loại output có ID lạ. Text tài liệu/model summary vẫn không tin cậy. Không đưa summary vào system role.
- Phân biệt user fact và assistant/model claim bằng `kind`/nhãn text; không biến câu model tự đoán thành kết quả tool đã chứng minh.
- Nguồn của batch: previous summary + closed messages trong range + task snapshot gắn `Task.request_message_id` vào range + bounded tool excerpts/source từ các task đó. Không load full tool JSON của mọi task vào prompt.
- Repo load theo conversation và ID range; query columns cần thiết, phân trang theo ID. Không giữ toàn bộ lịch sử/full tool payload của user trong RAM chỉ để kiểm tra budget.
- Recent context cũng nạp bounded snapshot của task gắn recent request IDs để lượt sau biết kết quả/công cụ đã dùng. Không tự động chạy lại task, không gắn task cũ thành task đang chạy.
- Legacy task không có request link: memory vẫn xem được theo user; không đoán turn cho compaction. Legacy tool_calls không task_id xử lý theo M5.

## M3 — Thuật toán compaction

1. Giữ guard của user từ Phase 2. Lưu request hiện tại, lấy ID rõ ràng; request đang xử lý không thuộc range compact.
2. Đọc previous summary + watermark; duyệt các **closed exchanges** có `message.id > watermark` theo ID tăng dần. Tính nhu cầu context trước khi Phase 1 trim.
3. Nếu context có thể vừa thì không gọi summarizer. Nếu cần loại history, chọn cutoff ở assistant terminal của exchange cũ. Mục tiêu giữ 4 exchanges gần nhất; đây là **soft target**. Nếu 4 không vừa thì giữ ít hơn, compact thêm exchange cũ; current request vẫn nguyên vẹn.
4. Batch mới là `(old_watermark, cutoff]`, không phải “older than watermark”. Gộp previous summary để không mất quyết định trước đó. Nếu chưa có eligible closed exchange, không compact; để builder xử lý overflow hợp đồng C3.
5. Trước **mỗi** summarizer call, dựng input riêng gồm stable summarizer instruction + previous summary + batch entries; cũng <=`context_max_chars`. Batch chứa message quá dài dùng source-linked excerpt với cờ omitted, không hứa toàn văn đã được giữ. Model nhận explicit yêu cầu giữ quyết định/lý do/kết quả/nguồn và user facts cần dùng lại; không được bịa nguồn.
6. Provider đang chọn chạy với summary JSON schema, không dùng `AgentAction`. Gọi ngoài DB transaction, không thêm schema-repair loop cho compaction. Tối đa **2 batch calls/turn** để tránh compaction tự tăng vô hạn; HTTP retry hiện tại không đổi. Mỗi response có usage đều được `record_llm_call(purpose='compact', user_id=...)` và `_TurnRecorder.add` ngay cả output invalid.
7. Nếu provider mock, hoặc summarizer lỗi/invalid: deterministic extractive fallback từ previous entries + messages user/assistant + tool source/error/result excerpts. Không loại mọi user message hoặc chỉ giữ assistant finals. Giữ nguyên ngôn từ đoạn trích, không suy luận quyết định/lý do không có trong input.
8. Quy tắc fit summary deterministic: deduplicate theo kind+source+text; giới hạn 2 entries/source trước; ưu tiên decision/user_fact có nguồn rồi reason/result/source, mới trước cũ trong cùng nhóm; xóa entry cuối cho tới khi vừa cap, tăng omitted_count. Đây là lossy policy, phải đo recall và khai báo giới hạn, không hứa giữ mọi fact.
9. Validate output, cap và ownership. Transaction ngắn mới cập nhật **summary và watermark cùng lúc**, điều kiện watermark vẫn bằng old value. Mismatch → bỏ kết quả update, không ghi đè state mới. Lỗi DB → rollback riêng transaction, không rollback turn đã ghi; không dùng session lỗi để tiếp tục.
10. Reload snapshot sau update và dựng context theo C1–C3. Dữ liệu `id <= watermark` không còn trong raw history đưa model; DB messages/tool rows **không bị xóa**. Không có eligible message mới → watermark không đổi, không tóm tắt lại chính summary.

Nếu cả model và fallback không tạo được summary hợp lệ: giữ nguyên summary/watermark; builder vẫn bound context; ghi sự kiện `compaction_skipped` không chứa raw text. Nếu còn backlog sau 2 batches, giữ watermark ở batch cuối đã lưu, ưu tiên recent tail và ghi rõ còn phần cũ chưa vào context. Không đánh dấu tất cả history đã compact để che backlog. Char budget của compaction tính input ứng dụng, adapter schema overhead đo riêng như Phase 1.

## M4 — Memory commands

Thêm sync APIs trong `services/memory.py`, nhận `telegram_user_id`, trả plain DTO; session mở/đóng bên trong worker. Repo tiếp tục nhận session đầu tiên. Handler chạy DB qua `asyncio.to_thread`, đăng ký trước `F.text`.

### `/memory`

- Chỉ private chat; group nhận hướng dẫn mở chat riêng, không gửi preview nhạy cảm. Identity từ middleware `from_user.id`, không nhận user_id tùy ý từ args.
- Scope: toàn bộ conversations của user, không phải chat_id; preview conversation mới nhất. Chưa có user/data → trả rỗng, không tạo user chỉ để xem.
- Hiển thị: số conversations/messages/tasks/tool calls có ownership; last activity; snapshot summary + omitted count; tối đa 2 recent exchanges, 3 task gần nhất kèm status/remaining và tối đa 2 tool source/result previews; tổng usage từ `repo.user_usage`.
- Mỗi preview hữu hạn, tổng tối đa 3 chunks qua `split_message`; không serialize full tool results vào Telegram. Phải có nội dung phiên, không chỉ thống kê token/cost.
- Đọc snapshot committed ở transaction ngắn; nếu worker đang chạy, ghi “đang xử lý, dữ liệu đến bước đã lưu gần nhất”. Không buộc read phải đợi LLM xong.

### `/forget` và `/forget confirm`

- Private chat; `/forget` chỉ cảnh báo scope giữ/xóa và hướng dẫn confirm. Parse bằng `Command('forget')` + `CommandObject.args`, chấp nhận đúng một token `confirm` (có whitespace bao quanh); `/forget@BotName confirm` hợp lệ. `confirm extra`, sai case hoặc args khác chỉ cảnh báo.
- Không cần nonce/TTL workflow mới cho sprint này. Đây là xác nhận bằng explicit command, không khẳng định có “confirmation session” đã lưu.
- `/forget confirm` acquire **cùng guard** với chat, atomically. Busy, kể cả sau `/cancel` trước worker exit → không xóa, báo chờ worker kết thúc rồi thử lại. Không tự cancel-and-wipe.
- Khi acquire được: xóa trong một transaction, commit thành công mới báo đã xóa. Giữ guard tới sau commit; turn bắt đầu sau khi guard nhả được phép tạo phiên mới. Không có worker trước-forget nào còn quyền ghi lại.
- Cả commands quota-exempt theo middleware hiện có. Cập nhật `/start` và `/help` theo semantics mới, không thay rate-limit policy.

## M5 — Ownership, thứ tự xóa và dữ liệu cũ

Trong một transaction, lấy toàn bộ conversation IDs và task IDs theo user, kể cả task có conversation_id NULL:

1. Kiểm tra ownership của LLM rows nối task: `user_id` hiện có mà khác task owner → abort với lỗi dữ liệu, không tự chiếm attribution. Backfill `LLMCall.user_id` còn NULL từ Task.user_id; detach `LLMCall.task_id=NULL` cho các task sắp xóa.
2. Xóa Traces thuộc owned conversations **hoặc** owned tasks; xóa Steps và ToolCalls theo owned task IDs.
3. Xóa Tasks trước Messages vì Phase 2 thêm Task.request_message_id; sau đó Messages theo owned conversations, cuối cùng Conversations. Không dựa vào cascade không tồn tại. Nếu gặp row tham chiếu chéo sai owner, FK phải chặn và toàn transaction rollback, không xóa lan sang user khác.
4. Giữ Users và toàn bộ LLMCalls. So sánh usage trước/sau không đổi calls/tokens/cost, không double count row vừa direct-user vừa task-linked.
5. Xóa lần hai là no-op thành công; không tạo conversation mới bên trong wipe.

**Legacy limitation bắt buộc công khai:** Sprint 2 ghi ToolCall.task_id=NULL và Trace không chứa tool_call_id, nên không thể quy thuộc tool rows cũ an toàn. Không match theo timestamp/params, không xóa toàn bộ NULL-task rows khi một user forget. Giữ các rows đó và báo rõ nếu DB còn orphan legacy rows: “Đã xóa bộ nhớ phiên có liên kết của bạn; dữ liệu công cụ cũ chưa định danh không thể xóa theo từng người dùng.” Số orphan toàn DB chỉ dành chẩn đoán admin, không hiển thị số liệu user khác. Nếu cần xóa sạch cả legacy, phải có thao tác quản trị được chủ dữ liệu chấp thuận riêng; không tự reset DB.

Scope xóa không gồm file trong `var/uploads`, file tool đã đọc, backup, lịch sử Telegram, provider-side retention hoặc usage accounting. Warning và hướng dẫn phải nêu giới hạn; không trả “mọi dữ liệu của bạn đã bị xóa”.

## File map (từ `Laplace-Demon/`)

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/models.py`, `laplace/db.py` | Summary/watermark columns, upgrade, per-connection FK |
| Modify | `laplace/repo.py` | ID-range history/task/tool snapshots, compare-and-update summary, memory view, wipe child-first |
| Modify | `laplace/context.py` | Summary schema/render/extractive fallback và budget batching thuần |
| Modify | `laplace/services/chat.py` | Trigger trước trim, provider compaction ngoài transaction, record usage |
| Modify | `laplace/services/memory.py` | View/forget service dùng guard Phase 2 |
| Modify | `laplace/bot/handlers.py` | Private memory commands, confirm parser, help |
| Modify | `tests/test_session_memory.py`, `tests/test_db.py`, `tests/test_chat_service.py` | Regression có DB thật tạm/worker thật |
| Modify | `README.md` | Commands, retention scope, migration/rollback và single-process constraint |

## Đầu việc

| ID | Việc | Effort | Xong khi |
|---|---|---|---|
| S3-09 | Summary migration + FK + history/source queries | 3h | Upgrade partial/fresh/legacy không mất data |
| S3-10 | Summary batching/validation/fallback/watermark | 5h | Multi-turn memory có nguồn; summary không lặp và input hữu hạn |
| S3-11 | Memory view + forget transaction + handlers | 4h | Private commands, đúng user, usage được giữ, legacy nói thật |
| S3-12 | Migration/race/failure regressions và Telegram demo | 4h | G3a/G3b có evidence, không chỉ mock response |

## Nghiệm thu G3a — Compaction

| ID | Kịch bản | Quan sát |
|---|---|---|
| M-01 | Fresh, Sprint 2 có rows, partial migration; init 2 lần | Đủ 3 cột mới xuyên Phase 2/3; dữ liệu và usage không mất; FK check sạch trên fixture hợp lệ |
| M-02 | Fact chỉ ở user turn 1; query lại sau 12 turns | Fact + nguồn còn trong input thực tế khi fixture vừa summary policy; context-aware provider chỉ trả đúng nếu nhận fact |
| M-03 | 2 lần compact liên tiếp, một lần không có eligible turn mới | Summary trước được merge; cutoff tăng đúng range mới; lần không có dữ liệu không gọi model |
| M-04 | 4 exchanges quá lớn; current request quá lớn | Giữ ít exchanges hơn có marker; request bắt buộc quá lớn dừng theo C3, không gửi vượt cap |
| M-05 | Model summary lỗi/invalid, fallback thành công; rồi lỗi cả fallback | Lần đầu dùng fallback có nguồn; lần sau summary+watermark cũ giữ nguyên; turn trước không bị rollback |
| M-06 | Tool fact/source chỉ nằm ToolCall, hai users giống text | Recall lấy đúng task/request/tool nguồn; không lẫn user; không invent attribution cho legacy |
| M-07 | Summary output dẫn source ID lạ hoặc có injected instruction | Không chấp nhận nguồn lạ; không đưa nội dung thành system instruction |
| M-08 | Nhiều batches và retry model đang chạy | Tối đa 2 summary calls/turn; usage compact được tính; không giữ write transaction trong network call |

## Nghiệm thu G3b — Memory/forget

- **M-09:** User A/B có nhiều conversations, tasks, traces, tools; A memory chỉ thấy A; group không lộ preview. User mới xem không sinh data.
- **M-10:** FK ON; A có task không conversation và llm calls direct-only/task-only/both: forget xóa đúng child rows, B nguyên vẹn, usage A không đổi, `foreign_key_check` sạch; repeat forget an toàn.
- **M-11:** Pause worker bằng Event; forget trong lúc chạy, sau cancel nhưng trước exit đều bị từ chối. Sau worker exit forget thành công; không có pre-forget writes xuất hiện muộn. Concurrent new chat/forget chỉ một được guard cho phép.
- **M-12:** Lệnh thường/confirm đúng/confirm sai/bot suffix/quota exhausted: đúng dispatch và deletion; không dựa vào raw text exact match.
- **M-13:** Legacy ownerless tool tồn tại: không xóa toàn cục, response nói rõ limitation; injected DB failure giữa wipe rollback toàn bộ, không báo thành công.
- **M-14:** Demo Telegram private: nêu fact → ≥12 turns → hỏi lại → tool turn → `/memory` → `/forget` không xóa → `/forget confirm` → `/memory` trống → hỏi fact không còn dữ liệu để trả → `/status` còn usage.

## Checklist và rủi ro

- [x] S3-09 đến S3-12 hoàn tất; regression S3-12 và demo Telegram M-14 đều đạt.
- [x] Giữ regression cho watermark, user-only fact, failure, FK, race và command parsing; không pin nguyên văn reply.
- [x] Demo Telegram M-14 đã được chủ dự án chạy trên tài khoản thật và xác nhận đạt; không dùng mock echo thay thế acceptance.
- [x] Không đụng DB thật/file upload trong smoke; backup và rollback có hướng dẫn.

Rủi ro: summary hữu hạn có thể bỏ facts; omission phải visible và được Phase 4 chấm, không coi summary là chân lý. `/forget` không bảo đảm xóa legacy vô chủ; phải nói rõ trước và sau confirm. Qua G3 mới bàn giao thực nghiệm Phase 4.
