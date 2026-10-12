# Phase 1: Context ba tầng và giới hạn tool observation

Status: Completed | Priority: P1 | Effort: 10h | Depends on: baseline Sprint 2

## Context và mục tiêu

- [Plan tổng](./plan.md) · [Phase 2](./phase-02-task-card.md) · [Phase 3](./phase-03-compaction-session-memory.md).
- `chat.py` hiện lưu request rồi query 10 messages, bỏ request bằng so sánh nội dung, dựng context một lần. `_next_action` thêm retry; vòng ngoài thêm action/observation.
- `repo.recent_messages` có SQL LIMIT: tăng budget trong builder không lấy lại được history đã bị query loại bỏ.
- Deliverable: mỗi lời gọi agent dùng context hữu hạn, đúng thứ tự và không mất tính toàn vẹn action/observation; DB giữ đầy đủ **payload do tool trả về**, không phải toàn bộ file nguồn nếu tool đã cắt.

## Contract kỹ thuật

### C1 — Dữ liệu và API nội bộ

`laplace/context.py` là module thuần: không DB, không gọi provider. Dùng dataclass nhỏ cho kết quả dựng context và nhóm message; không tạo hierarchy strategy/service.

```python
build_context(
    *, system_message, history_groups, request_message,
    execution_groups, task_card=None, rolling_summary=None,
    max_chars=12_000,
) -> ContextBuild
# ContextBuild: messages, char_count, dropped_group_count, overflow_reason
```

- Tất cả inputs là plain values, không ORM. `max_chars` truyền rõ; Settings khởi tạo ở biên service, không đọc `.env` trong builder.
- History group = một request user + assistant terminal response tương ứng; nhóm incomplete được ghi nhãn, không ghép nhầm hai requests. ID dùng để chọn phạm vi; hai requests trùng text vẫn là hai lượt khác nhau.
- Execution group = action JSON + observation; retry group = invalid response + validation error. Không cắt thành message mồ côi. Phân biệt rõ history đã đóng và execution đang chạy.
- Trả danh sách mới mỗi call; không mutate history hoặc message list đã đưa provider. Observer/test phải snapshot đầu vào tại thời điểm gọi.

### C2 — Thứ tự và ranh giới tin cậy

1. Layer 1: `prompts.system_message()` chứa stable policy + tool specs, giữ nguyên.
2. Layer 2: tối đa một message role `user`, nhãn rõ `SESSION_STATE_DATA`; JSON-encode card/summary, coi toàn bộ phần này là dữ liệu hồi tưởng, không phải yêu cầu mới. Policy ở layer 1 nói rõ dữ liệu này không được thay thế chỉ dẫn hệ thống.
3. Layer 3: history tail → current request đúng một lần → execution groups của lượt hiện tại → retry group mới nhất nếu có. **Current request không còn ở cuối sau khi tool đã chạy**; observation/retry mới nhất phải ở cuối.

Không nhét raw user/tool text vào system-role. Điều chỉnh serialization của `observation_message` để payload có chuỗi `</tool_output>` không phá envelope: JSON-encode và escape delimiter characters trong giá trị dữ liệu. Đây là bảo toàn ranh giới dữ liệu, không tuyên bố đã giải quyết toàn bộ prompt injection của Sprint 5.

### C3 — Ngân sách và overflow

- `Settings.context_max_chars` mặc định 12.000, integer > 0; `.env.example`: `LAPLACE_CONTEXT_MAX_CHARS=12000`.
- Định nghĩa chính xác cho production/strategy `sprint3`: `sum(len(m['content']) for m in messages) <= max_chars` trước mọi `provider.complete`, gồm agent_action và schema_retry. Đếm cả nhãn/delimiter/truncation marker do ứng dụng thêm. A/B chỉ trong thực nghiệm có policy không cap như Phase 4; không áp C3 rồi gọi đó là baseline nguyên trạng.
- Đây **không** phải giới hạn token/context window của provider. `OpenAIProvider.complete:115-129` thêm schema instruction riêng; giữ adapter contract, đo overhead đó riêng ở Phase 4. Không viết “12k chars ≈ 3k tokens” như bảo đảm.
- Giữ nguyên stable system + current request; giữ latest execution/retry group sau khi đã bounded dữ liệu. Card tối đa 1.000 chars, summary view tối đa 2.000 chars; cap bao gồm wrapper và marker.
- Retry diagnostics là dữ liệu dẫn xuất: invalid response preview <=1.000 chars, validation error preview <=500 chars, mỗi cap tính cả wrapper/marker; giữ cặp cùng nhau. Action JSON hợp lệ không bị cắt thành JSON hỏng; nếu latest action có params quá lớn không thể giữ cùng phần bắt buộc, đi đường overflow thay vì âm thầm đổi params.
- Thứ tự giảm: history groups cũ nhất → execution groups cũ nhất (metadata vẫn có trong card) → summary entries ít ưu tiên → card fields tùy chọn. Không xóa goal/status/tool count cốt lõi khỏi card.
- Nếu phần bắt buộc vẫn không vừa: `overflow_reason='required_context_too_large'`, service trả thông báo rút ngắn yêu cầu/đổi cấu hình, ghi trace `context_overflow`, **không gọi provider hoặc thực thi tool tiếp**. Phase 2 đóng task `failed` nếu đã có task.
- Trước Phase 3: phần history bị loại chỉ mất khỏi prompt, vẫn trong DB; chưa quảng cáo có recall dài hạn. Phase 3 compact trước khi builder loại phần cũ.

### C4 — Tool payload

- `repo.record_tool_call` trả row đã flush; lưu JSON `{ok, payload}` đầy đủ, bỏ `payload[:2000]`; `payload` giữ representation hiện dùng để không tự ý đổi tool API.
- Observation giữ tool name, ok/error, nguồn từ params (ví dụ file path), `tool_call_id`, tổng độ dài và cờ truncated. Success và error đều có giới hạn.
- Cap toàn bộ observation là 1.200 chars. Ưu tiên metadata nguồn + status; phần text còn lại chia head/tail gần bằng nhau, giữ marker và độ dài bị bỏ; áp dụng cap **sau escaping**. Nếu wrapper/params dài, chỉ đưa source preview hữu hạn, params đầy đủ vẫn trong DB.
- Đây là excerpt deterministic, không phải semantic summary. Fact ở giữa có thể mất; Phase 4 phải đo trường hợp này. Không tự tạo thêm LLM call cho mỗi tool.
- DB ID là reference cho quan sát/báo cáo; không chỉ dẫn model “đọc DB ID” khi registry không có tool đó. Không mở rộng `read_file` hoặc thêm retrieval tool trong sprint này.
- Phase 1 chỉ là checkpoint nội bộ: full payload chưa được public demo trước Phase 2 gắn ownership cho mọi tool row mới.

### C5 — Shared runtime để đánh giá công bằng

- Giữ `handle_message(telegram_user_id, username, text, progress=None)` làm wrapper production.
- Tách thân vào một hàm nội bộ `_handle_message(..., *, provider, context_strategy, context_observer=None)`; wrapper luôn chọn `sprint3`, provider từ cấu hình. Không tạo public CLI/env strategy switch, không fork vòng agent.
- `context_strategy` là Literal nhỏ `full | window10 | sprint3`, kiểm soát **history query, compaction, card injection và observation rendering**, không chỉ budget. A/B chỉ dùng bởi runner Phase 4; schema, tool executor, persistence, step/retry limits vẫn chung.
- Observer tùy chọn nhận snapshot messages, purpose, char count, strategy và metadata truncation; không log nội dung production nếu không truyền observer. Chỉ đưa callback ở ranh giới provider, không thêm telemetry subsystem.
- A/B/C cụ thể ở Phase 4; không implement ba bản chat service.

## File map (đường dẫn từ code root `Laplace-Demon/`)

| Action | File | Thay đổi |
|---|---|---|
| Create | `laplace/context.py` | Pure grouping, builder, bounded excerpt, overflow result |
| Modify | `laplace/config.py`, `.env.example` | Một setting budget |
| Modify | `laplace/prompts.py` | Giữ policy/schema/text builders; bỏ `build_turn_messages` sau cutover; envelope dữ liệu |
| Modify | `laplace/services/chat.py` | Shared-runtime seam; context factory gọi trước từng provider attempt; current-request ID |
| Modify | `laplace/repo.py` | Query history theo conversation + ID range, chronological grouping; full/window query |
| Modify | `tests/test_chat_service.py` | Chuyển caller bị ảnh hưởng; regression qua consumer |
| Create | `tests/test_context.py` | Boundary/overflow/group-preservation có rủi ro thực |

Không sửa `tools/base.py` chỉ để thêm comment. Trước thay đổi exported symbols, tra references bằng LSP nếu có; cập nhật tất cả callers, không để shim `build_turn_messages` cũ.

## Đầu việc và thứ tự

| ID | Việc | Effort | Xong khi |
|---|---|---|---|
| S3-01 | Chốt nhóm messages, overflow result, setting và schema budget boundary | 2h | Ví dụ first-call/tool-call/retry có kết quả thứ tự và size xác định |
| S3-02 | Builder + bounded observation + data envelope | 3h | Nhiều payload dài không phá nhóm hoặc vượt cap |
| S3-03 | Query theo ID, shared runtime, dựng trước mọi attempt | 3h | Current request đúng một lần; retries và 5 tools đều đi qua builder |
| S3-04 | Smoke offline và regression tại ranh giới | 2h | Bằng chứng G1 trong ma trận dưới |

## Nghiệm thu G1

| ID | Kịch bản | Kết quả quan sát bắt buộc |
|---|---|---|
| C-01 | History dài; hai requests có text giống nhau | Mỗi request giữ đúng ID/turn; request hiện tại chỉ xuất hiện một lần |
| C-02 | 5 tools với payload lớn, xen 2 schema retries | Snapshot **mọi** agent call <= budget; không mồ côi action/observation |
| C-03 | Phần bắt buộc đúng cap và vượt cap 1 char | Đúng cap được gọi; vượt cap dừng, provider không nhận request quá lớn |
| C-04 | Payload >2.000 chars, Unicode và literal `</tool_output>` | DB round-trip toàn payload; observation <=1.200 chars và data không thành system message |
| C-05 | Tail chứa fact, giữa chứa fact, lỗi tool dài | Excerpt/cờ missing đúng sự thật; không khẳng định fact giữa được giữ |
| C-06 | Snapshot đã ghi, sau đó thêm bước/retry | Snapshot cũ không đổi; metric peak có căn cứ theo từng call |

Smoke chạy `_handle_message` với provider offline, DB SQLite tạm và tool fixture thật; quan sát requests và dữ liệu ghi, không chỉ gọi test builder. Regression giữ C-01 đến C-04/C-06 nơi có plausible bug; không tạo test chỉ pin tiêu đề/wording. Cuối toàn sprint mới chạy full suite/lint một lần.

## Checklist và rủi ro

- [x] S3-01 đến S3-04 hoàn tất; bằng chứng G1 trong regression và offline smoke schema v2.
- [x] Không còn caller production của `build_turn_messages`.
- [x] Không query LIMIT 10 trước đường `full`/`sprint3`; đường production dùng tail hữu hạn sau compaction.
- [x] Không tuyên bố hard token budget hoặc mọi fact đều nằm trong excerpt.
- [x] Trước demo full payload: Phase 2 hoàn tất ownership.

Rủi ro chính: excerpt mất fact giữa; adapter thêm schema ngoài char cap. Đo công khai ở Phase 4, không che bằng mock trả đáp án. Bàn giao C1–C5 cho Phase 2, không mở rộng tool registry để xử lý rủi ro trong sprint này.
