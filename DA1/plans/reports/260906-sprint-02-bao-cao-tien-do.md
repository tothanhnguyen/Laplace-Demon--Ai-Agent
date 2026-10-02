# Báo Cáo Tiến Độ Sprint 2 (24/08 – 06/09)

**Mục tiêu sprint (theo đề cương):** Kênh giao tiếp Telegram và kết nối mô hình ngôn ngữ.
**Sản phẩm bàn giao:** Bot Telegram hoạt động với mô hình ngôn ngữ thật; theo dõi được mức sử dụng và chi phí; đổi được nhà cung cấp mô hình mà không sửa mã nguồn.

Báo cáo này thay thế `plans/260824-0935-agent-loop-stop-reason/sprint-02-progress-report.md` (file đó mô tả phần vòng lặp Agent — thuộc phạm vi Sprint 1).

## 1. Việc đã hoàn thành theo từng hạng mục

### Tuần 1 — Kênh giao tiếp Telegram

**S2-01 — Bot Telegram: tin nhắn, lệnh, tệp đính kèm** ✅
- `laplace/bot/handlers.py`: router aiogram 3.x với `/start`, `/help`, `/status`, `/cancel`; handler văn bản nối vào agent; handler `document` tải tệp về `var/uploads/` (giới hạn 10MB, sanitize tên file — tóm tắt nội dung tệp thuộc Sprint 4, bot nói rõ điều này thay vì giả vờ xử lý).
- `laplace/bot/runner.py`: khởi động long polling, tự `init_db()` trước khi chạy; thiếu token báo lỗi kèm hướng dẫn @BotFather.
- Entrypoint: `python -m laplace --bot`.

**S2-02 — Gắn người dùng với phiên làm việc riêng** ✅
- `laplace/bot/middleware.py` chuẩn hóa identity (telegram_user_id, username) cho mọi update.
- `laplace/models.py` + `laplace/repo.py`: mỗi tin nhắn gắn vào `conversation` của đúng `user` (`get_or_create_user` / `get_or_create_conversation`).
- Test: hai user chat song song không lẫn dữ liệu (`tests/test_db.py`).

**S2-03 — Phản hồi dài, trạng thái đang xử lý, tiến độ từng bước** ✅
- `laplace/bot/textsplit.py`: chia tin nhắn >4096 ký tự theo ranh giới dòng/khoảng trắng, ghép lại bằng nguyên bản (có test).
- Luồng xử lý: typing action → tin "Đang xử lý..." → edit tin đó theo callback tiến độ từng bước từ agent (thread-safe qua `call_soon_threadsafe`, coalesce khi tiến độ dồn dập).
- `/cancel` hủy được lượt đang chạy (registry `asyncio.Task` theo user).

**S2-04 — Giới hạn tần suất yêu cầu** ✅
- `laplace/bot/ratelimit.py`: token bucket theo user_id, mặc định 5 request/phút; tin bị chặn nhận thông báo lịch sự kèm số giây chờ. Lệnh `/` không tính quota để `/cancel`, `/status` luôn dùng được.
- 14 test cho ratelimit + textsplit (`tests/test_bot_helpers.py`), không phụ thuộc aiogram.

### Tuần 2 — Kết nối mô hình ngôn ngữ thật

**S2-05 — LLM thật, đổi provider bằng cấu hình** ✅
- `laplace/llm/presets.py`: registry 3 nhà cung cấp (Gemini, OpenAI, Groq) — base_url, model mặc định, bảng giá, trang lấy key.
- `laplace/llm/openai_provider.py`: một adapter dùng chung cho mọi endpoint tương thích OpenAI.
- Đổi provider = sửa `LAPLACE_LLM_PROVIDER` trong `.env`, không sửa mã nguồn. Thiếu key → lỗi thân thiện kèm URL trang lấy key đúng hãng.
- `laplace/llm/mock.py`: provider offline cho CI/test/dev không cần key.

**S2-06 — Ghi usage/chi phí từng lời gọi; thử lại khi quá tải** ✅
- Mỗi lời gọi LLM ghi vào bảng `llm_calls`: purpose, provider, model, prompt/completion tokens, cost USD (tính theo bảng giá trong preset), latency ms, user_id.
- `/status` trên bot và `usage=` trên CLI đọc tổng hợp từ `repo.user_usage()` (aggregate SQL, nhóm rỗng trả 0).
- Retry khi HTTP 429: tối đa 5 lần, thời gian chờ đọc từ hint "retry in Xs" của provider hoặc backoff tăng dần, trần 90s (tự viết, không cần tenacity).

**S2-07 — Chỉ dẫn hệ thống đầu tiên + gọi công cụ có kiểm tra dữ liệu** ✅
- `laplace/prompts.py`: system prompt v1 (chính sách + danh sách tool tự sinh từ registry); LLM trả JSON theo schema `AgentAction`.
- Hai tầng validate Pydantic: (1) output LLM validate bằng `AgentAction` — sai schema thì gửi thông điệp lỗi để mô hình tự sửa, tối đa 2 lần, quá thì dừng lượt với lời giải thích trung thực; (2) tham số tool validate bằng model riêng của tool trước khi chạy (`laplace/tools/base.py`).
- Kết quả tool đưa vào context trong delimiter `<tool_output>` và được đánh dấu là dữ liệu, không phải lệnh (nền tảng cho phòng chống injection ở Sprint 5).
- Tool mẫu: `read_file` (giới hạn trong thư mục làm việc, trần 64KB).

**S2-08 — Kiểm thử toàn tuyến** ✅ (phần mock) / ⏳ (demo model thật)
- `tests/test_chat_service.py`: toàn tuyến lượt chat bằng MockLLM scripted — trả lời trực tiếp, gọi tool rồi kết thúc, self-correction sai schema, dừng trung thực khi sai quá số lần, ghi llm_calls đúng user. CI không cần API key.
- Smoke test đã chạy: `python -m laplace --chat "xin chào, bạn là ai?"` → tiến độ từng bước, trả lời, usage; DB sau đó có đúng 1 user, 1 conversation, 2 messages, 1 llm_call, 1 trace.
- ⏳ Còn lại: demo với model thật + ghi lại 3 hội thoại demo — cần 2 thứ từ bạn: `LAPLACE_TELEGRAM_BOT_TOKEN` (tạo qua @BotFather) và `LAPLACE_GEMINI_API_KEY` (free, aistudio.google.com/apikey). Dán vào `.env` rồi `python -m laplace --bot` là chạy.

### Nợ Sprint 1 đã trả trong sprint này

- **S1-08 — Lớp lưu trữ:** `laplace/db.py` (engine, session_scope) + `laplace/models.py` — đủ 8 bảng theo ERD: users, conversations, messages, tasks, steps, tool_calls, llm_calls, traces. SQLAlchemy 2.0 typed ORM, SQLite.
- **S1-10 — Nhật ký từng bước:** bảng `traces` ghi JSON mỗi bước của lượt chat (tool_call/final/step_limit/schema_failure) kèm conversation_id, step_index.

## 2. Bằng chứng kiểm tra

| Kiểm tra | Lệnh | Kết quả |
| --- | --- | --- |
| Lint | `ruff check .` | All checks passed |
| Toàn bộ test | `pytest` | **39 passed** (5 db + 8 chat toàn tuyến + 14 bot helpers + 12 agent/bootstrap cũ) |
| CLI mặc định | `python -m laplace` | chạy OK |
| Agent mẫu Sprint 1 | `python -m laplace --sample-agent` | `status=completed` + lý do dừng |
| Lượt chat toàn tuyến | `python -m laplace --chat "..."` | trả lời + usage; DB ghi đủ messages/llm_calls/traces |
| Bot thiếu token | `python -m laplace --bot` | RuntimeError kèm hướng dẫn @BotFather (không crash khó hiểu) |

Toàn bộ test chạy bằng MockLLM + SQLite in-memory: CI GitHub Actions không cần secret nào.

## 3. Quyết định kỹ thuật đáng chú ý

1. **Một adapter cho mọi provider** (OpenAI-compatible endpoint + `base_url`) thay vì SDK riêng từng hãng — đổi provider thuần config, đúng yêu cầu S2-05.
2. **Không dùng strict structured-outputs** của OpenAI (schema Pydantic mặc định không thỏa điều kiện strict → 400). Thay bằng `json_object` mode + schema nhúng system message + validate Pydantic ở tầng agent với self-correction.
3. **`llm_calls` có cột `user_id`** (ngoài `task_id` nullable) để `/status` tính usage theo người dùng ngay cả với lời gọi không gắn task.
4. **Giới hạn 5 bước tool/lượt** giữ nguyên nguyên tắc chống chạy vô hạn từ Sprint 1; chạm trần thì dừng và nói rõ.
5. **Bot trung thực về phạm vi:** nhận tệp thì lưu và báo tóm tắt thuộc sprint sau, không giả vờ xử lý.

## 4. Giới hạn đã biết (chuyển sprint sau)

- Context chưa có ba tầng/rút gọn (Sprint 3); mới nạp 10 message gần nhất.
- Tool chưa có timeout/phân loại rủi ro/Approval Gate (Sprint 4–5).
- Trang quan sát trace chưa có (Sprint 7); trace hiện xem bằng SQLite.
- Demo hội thoại model thật chưa quay — chờ token + API key.

## 5. Việc cần bạn làm để chốt bàn giao

1. Tạo bot qua @BotFather → dán `LAPLACE_TELEGRAM_BOT_TOKEN` vào `Laplace-Demon/.env`.
2. Lấy key Gemini free → `LAPLACE_GEMINI_API_KEY`, đặt `LAPLACE_LLM_PROVIDER=gemini`.
3. `python -m laplace --bot`, chat thử và chụp/ghi lại 3 hội thoại demo cho GV (gợi ý: chào hỏi; hỏi kiến thức thường; nhờ đọc một file trong thư mục chạy để thấy tool calling).
4. `/status` trên bot để cho GV xem số liệu chi phí thật.
