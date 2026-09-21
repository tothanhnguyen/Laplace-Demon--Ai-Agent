# Hướng dẫn nguyên lý Sprint 3

## Mục tiêu

Sprint 3 thay cửa sổ 10 message bằng context có ngân sách, lưu task/tool theo user, rút gọn hội thoại dài và cho người dùng xem/xóa bộ nhớ phiên.

## Context ba tầng

`laplace/context.py` dựng messages trước từng lời gọi agent:

1. **Quy tắc ổn định:** system prompt và tool schema.
2. **Trạng thái phiên:** task card và rolling summary, đóng gói dưới nhãn dữ liệu không tin cậy.
3. **Diễn biến mới:** history gần nhất, request hiện tại, các cặp action/observation và schema correction.

`LAPLACE_CONTEXT_MAX_CHARS` mặc định 12.000. Đây là số ký tự nội dung do ứng dụng dựng, không phải hard token limit của provider. System policy, request hiện tại và causal unit mới nhất không bị cắt rời. Nếu phần bắt buộc vẫn vượt giới hạn, service dừng trước khi gọi model và yêu cầu rút ngắn request hoặc tăng cấu hình.

Mỗi tool result được lưu đầy đủ theo dữ liệu **tool đã trả về** trong `tool_calls.result_json`. Model chỉ nhận JSON excerpt tối đa 1.200 ký tự, gồm trạng thái, source, độ dài và `tool_call_id` để audit. ID không cho model quyền tự đọc lại DB.

## Task card và vòng đời

Một turn chỉ tạo `Task` khi model yêu cầu tool đầu tiên. `Task.request_message_id` gắn task vào đúng request; mọi `Step`, `ToolCall` và `Trace` sau đó có `task_id`.

Task card lấy dữ liệu đã persist để hiển thị:

- mục tiêu;
- tiêu chí hoàn thành do model đề xuất;
- decision hiện tại và số tool thực sự đã chạy;
- tool thành công/thất bại;
- phần việc còn lại;
- trạng thái terminal.

`completed` chỉ có nghĩa model đã trả final. Lớp kiểm chứng độc lập thuộc Sprint 6.

Bot và service dùng reservation process-local theo Telegram user ID. `/cancel` đặt cờ cooperative cancellation; lời gọi model/tool đang chạy hoàn tất rồi worker mới dừng tại checkpoint. Hệ thống hiện hỗ trợ một process bot, không phải distributed lock nhiều process.

## Compaction

Khi raw history không còn vừa budget, service giữ tối đa bốn exchange gần nhất và compact oldest-first tối đa hai page, mỗi page tối đa tám message projection hữu hạn, theo khoảng:

```text
watermark cũ < message.id <= cutoff mới
```

Summary JSON lưu tối đa 10 entries. Mỗi entry có loại (`decision`, `reason`, `result`, `source`, `user_fact`) và ID nguồn message/tool. Query compaction chỉ lấy cột cần thiết; message/tool payload lớn được dựng thành excerpt head/tail trước khi rời DB. Provider thật được yêu cầu trả summary có schema; mock, lỗi provider hoặc output không hợp lệ dùng fallback extractive deterministic.

Summary và watermark cập nhật cùng một statement compare-and-update, sau khi usage của compaction call đã ghi thành công. Nếu hai page chưa xử lý hết gap trước recent tail, context ghi `UNSUMMARIZED_HISTORY_BACKLOG`; lượt sau tiếp tục từ watermark, không âm thầm coi phần gap đã được tóm tắt.

Compaction không xóa messages/tool results gốc. Summary hữu hạn có thể bỏ fact; `omitted_count`, backlog marker và báo cáo thực nghiệm phản ánh giới hạn này.

## Lệnh Telegram

Các lệnh chỉ hiển thị nội dung memory trong private chat:

- `/memory`: số conversation/message/task/tool, usage, recent exchanges, summary và task/tool previews.
- `/forget`: cảnh báo phạm vi, chưa xóa.
- `/forget confirm`: xóa memory có ownership trong một transaction.
- `/cancel`: yêu cầu worker dừng ở checkpoint.

Forget xóa child-first: traces/steps/tool calls → tasks → messages → conversations. `users` và `llm_calls` được giữ; LLM calls gắn task được backfill `user_id` rồi detach task trước khi xóa. Usage trước/sau phải bằng nhau.

Giới hạn retention:

- Không xóa file trong `var/uploads`, file nguồn tool đã đọc, Telegram history, provider retention hoặc backup.
- Tool logs Sprint 2 có `task_id=NULL` không thể quy thuộc user an toàn nên được giữ và thông báo rõ.
- Forget bị từ chối khi worker của user còn chạy, kể cả đã yêu cầu `/cancel` nhưng worker chưa thoát.

## Migration và rollback

`init_db()` vẫn tạo schema mới bằng SQLAlchemy, sau đó chạy migration SQLite additive/idempotent cho:

- `tasks.request_message_id`;
- `conversations.summary`;
- `conversations.summary_until_message_id`.

Mỗi SQLite connection bật `PRAGMA foreign_keys=ON`. Trước nâng cấp DB quan trọng, dừng app và tạo backup nhất quán bằng SQLite backup API; rollback bằng code tương ứng và restore backup đã kiểm tra, không xóa `laplace.db` mặc định.

## Thực nghiệm A/B/C

Chạy offline, không cần API key:

```bash
.venv/bin/python scripts/eval_context.py \
  --mode offline --strategies full,window10,sprint3 --repeat 2
```

Runner dùng cùng chat runtime cho ba policy:

- `full`: toàn history và full tool observation;
- `window10`: đúng baseline tối đa 9 rows cũ + request hiện tại;
- `sprint3`: summary + recent history + bounded tool observation.

Offline probe schema v2 chỉ đo evidence availability, application chars, logical calls và cơ chế compaction. Scorer chuẩn hóa case/whitespace, chấm association nguồn↔fact và từ chối phủ định; nó không chứng minh chất lượng model, token hoặc chi phí.

Live run cuối dùng một provider/model xuyên suốt:

```bash
.venv/bin/python scripts/eval_context.py \
  --mode live --provider router9 \
  --strategies full,window10,sprint3 --repeat 1 \
  --max-calls 600 --stop-after-observed-usd 0 --pacing-seconds 0.2
```

Artifact `20260916T145221Z` hoàn tất 24/24 case-strategy,
`incomplete=false`: full 8/8 với 255.950 tokens, window10 4/8 với 138.819
tokens và sprint3 7/8 với 175.878 tokens. `cost_usd=0` là chi phí biên của
route subscription local, không bao gồm phí thuê bao hoặc quota.

Ngưỡng USD dựa trên chi phí đã quan sát, không phải hard billing cap.
`--pacing-seconds` nhịp đều logical calls; call thiếu usage/giá hoặc row
terminal không-completed làm run `incomplete`. Manifest schema v2 ghi
`source_sha256`, `fixture_sha256`, provider/model thực trả và stop reason.

## File map

- `laplace/context.py`: grouping, budget, tool excerpt, task card, summary schema/fallback.
- `laplace/services/chat.py`: shared runtime, task lifecycle, compaction và persistence.
- `laplace/services/memory.py`: reservation, cancellation, memory view/forget.
- `laplace/repo.py`, `laplace/models.py`, `laplace/db.py`: ownership, queries, migration và atomic deletion.
- `laplace/bot/handlers.py`: Telegram commands và worker lifecycle.
- `experiments/context_eval_tasks.py`, `scripts/eval_context.py`: bộ 8 cases và runner.
