# Laplace's Demon

AI Agent cá nhân giao tiếp qua Telegram, phát triển theo từng sprint.
Sprint 4 bổ sung tool contracts strict, Tavily Search/Extract có citation,
đọc TXT/PDF/DOCX theo locator và reminder một lần có persistence/recovery.
Identity, destination, timezone và attachment paths chỉ đến từ trusted runtime context.

## Yêu cầu

- Python 3.11 trở lên

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

## Cấu hình

Các biến đều có tiền tố `LAPLACE_` (đặt trong `.env`):

| Biến | Mặc định | Mục đích |
| --- | --- | --- |
| `LAPLACE_LLM_PROVIDER` | `mock` | Nhà cung cấp LLM: `mock`, `gemini`, `openai`, `groq`, `bai`, `router9` |
| `LAPLACE_LLM_MODEL` | (trống) | Override model; trống thì dùng mặc định của provider |
| `LAPLACE_GEMINI_API_KEY` | (trống) | Key Gemini — lấy tại aistudio.google.com/apikey (free tier) |
| `LAPLACE_OPENAI_API_KEY` | (trống) | Key OpenAI |
| `LAPLACE_GROQ_API_KEY` | (trống) | Key Groq |
| `LAPLACE_BAI_API_KEY` | (trống) | Key B.AI cho endpoint OpenAI-compatible `https://api.b.ai/v1` |
| `LAPLACE_ROUTER9_API_KEY` | (trống) | Key gateway local 9Router tại `http://127.0.0.1:20128/v1` |
| `LAPLACE_DATABASE_URL` | `sqlite:///laplace.db` | Nơi lưu phiên, tin nhắn, log LLM/tool/trace |
| `LAPLACE_TELEGRAM_BOT_TOKEN` | (trống) | Token bot từ @BotFather |
| `LAPLACE_CONTEXT_MAX_CHARS` | `12000` | Ngân sách ký tự messages do ứng dụng dựng; không phải hard token limit |
| `LAPLACE_TAVILY_API_KEY` | (trống) | Key Tavily cho Web Search/Extract |
| `LAPLACE_HTTP_TIMEOUT_SECONDS` | `10` | Timeout HTTP tối đa cho tool web |
| `LAPLACE_WEB_MAX_RESULTS` | `5` | Số kết quả web tối đa mỗi lần tìm |
| `LAPLACE_WEB_MAX_CHUNKS` | `3` | Số đoạn evidence tối đa mỗi nguồn |
| `LAPLACE_DOCUMENT_MAX_BYTES` | `10485760` | Trần upload tài liệu 10 MiB |
| `LAPLACE_DOCUMENT_MAX_UNITS` | `200` | Số đơn vị tài liệu tối đa được index |
| `LAPLACE_DOCUMENT_UNIT_MAX_CHARS` | `4000` | Trần ký tự mỗi đơn vị tài liệu |
| `LAPLACE_DOCUMENT_FULL_SUMMARY_MAX_CHARS` | `4000` | Ngân sách text mỗi lượt đọc |
| `LAPLACE_DOCUMENT_PARSE_TIMEOUT_SECONDS` | `10` | Timeout parser PDF/DOCX subprocess |
| `LAPLACE_DOCUMENT_PARSER_MEMORY_MB` | `256` | Giới hạn memory parser trên POSIX |
| `LAPLACE_DOCUMENT_MAX_EXPANDED_BYTES` | `52428800` | Trần expanded bytes cho DOCX ZIP |
| `LAPLACE_DOCUMENT_MAX_ZIP_ENTRIES` | `1000` | Trần số ZIP entries của DOCX |
| `LAPLACE_TIMEZONE` | `Asia/Ho_Chi_Minh` | IANA timezone dùng để hiểu lịch reminder |
| `LAPLACE_REMINDER_SCAN_INTERVAL_SECONDS` | `1` | Chu kỳ quét reminder đến hạn |
| `LAPLACE_REMINDER_MISFIRE_GRACE_SECONDS` | `3600` | Grace phục hồi reminder sau restart |
| `LAPLACE_REMINDER_MAX_HORIZON_DAYS` | `365` | Khoảng đặt reminder tối đa |
| `LAPLACE_REMINDER_MAX_ATTEMPTS` | `3` | Số lần delivery tối đa |
| `LAPLACE_REMINDER_SEND_TIMEOUT_SECONDS` | `10` | Timeout một lần gửi Telegram |
| `LAPLACE_REMINDER_BATCH_SIZE` | `25` | Số reminder tối đa mỗi lượt quét |
| `LAPLACE_REMINDER_MAX_CONCURRENCY` | `5` | Số delivery đồng thời tối đa |

Đổi nhà cung cấp mô hình chỉ cần sửa `.env`, không sửa mã nguồn. Thiếu key thì
chương trình báo đúng trang lấy key của hãng tương ứng.

Ví dụ cấu hình B.AI OpenAI-compatible:

```env
LAPLACE_LLM_PROVIDER=bai
LAPLACE_LLM_MODEL=gpt-5.2
LAPLACE_BAI_API_KEY=sk-...
```

Chỉ lưu key thật trong `.env` đã bị Git bỏ qua; không ghi key vào
`.env.example`, lệnh shell, log hoặc artifact thực nghiệm.

Fallback qua 9Router local đang chạy:

```env
LAPLACE_LLM_PROVIDER=router9
LAPLACE_LLM_MODEL=cx/gpt-5.6-sol
LAPLACE_ROUTER9_API_KEY=...
```

Copy key từ dashboard `http://127.0.0.1:20128/dashboard`. Route Codex dùng
subscription nên `cost_usd` là chi phí biên `0`; phí thuê bao và quota không
được quy đổi theo token.

## Chạy bot Telegram

1. Tạo bot với [@BotFather](https://t.me/BotFather), copy token.
2. Đặt `LAPLACE_TELEGRAM_BOT_TOKEN` và key LLM vào `.env`.
3. Chạy:

```bash
python -m laplace --bot
```

Lệnh bot: `/start`, `/help`, `/status` (usage + chi phí), `/memory` (xem
memory và reminder trong private chat), `/forget` và `/forget confirm` (xóa
memory/reminder có liên kết nhưng giữ usage), `/cancel` (cooperative cancellation).
Bot đọc TXT/PDF/DOCX tối đa 10 MiB, gọi Tavily khi cần dữ liệu web có nguồn,
hiển thị task card theo bước và chia phản hồi quá 4096 ký tự.

Web provider decision: Sprint 4 dùng trực tiếp Tavily Search + Extract REST.
Không cache, không local full-page fetch và không browser fallback; URL nguồn
thành công duy nhất mới được đưa vào `sources`.

Reminder chỉ nhận capability từ directive rõ ràng trong message Telegram private:
`/remind`, `/remind_update`, `/remind_cancel` (directive nhiều bước đặt sau `;`).
Web/document tool data hoặc câu văn chỉ nhắc đến reminder không thể tự cấp
capability này. Delivery dùng
at-least-once với retry hữu hạn và stable reminder ID: Telegram không có
idempotency key nên lỗi/crash mơ hồ có thể tạo bản gửi trùng; hệ thống không
tuyên bố exactly-once.

## Chạy một lượt chat qua CLI

```bash
python -m laplace --chat "xin chào, bạn là ai?"
```

In tiến độ từng bước, câu trả lời cuối và usage (`llm_calls`, tokens, cost).
Với `LAPLACE_LLM_PROVIDER=mock` lệnh chạy offline không cần key.

## Chạy nhiệm vụ mẫu (Sprint 1)

```bash
python -m laplace --sample-agent
```

Kết quả gồm `status` (`completed` / `failed` / `step_limit`) và
`completion_reason` giải thích lý do dừng.

## Kiến trúc (Sprint 4 — Phase 1)

- `laplace/context.py` — context ba tầng, char budget, bounded tool observation,
  task card và rolling-summary schema.
- `laplace/services/chat.py` — shared agent runtime; tối đa 5 tool executions;
  ghi task/step/tool/trace ownership và compact history theo watermark.
- `laplace/services/memory.py` — một worker process-local mỗi user,
  cooperative cancellation, memory snapshot và atomic forget.
- `laplace/llm/` — provider OpenAI-compatible, retry 429, usage/cost và mock offline.
- `laplace/tools/` — registry strict với input/output schema, trusted context,
  structured failure và source metadata; tool theo nghiệp vụ được thêm từ Sprint 4.
- `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` — SQLAlchemy/SQLite,
  migration additive, FK checks và child-first deletion.
- `laplace/bot/` — aiogram 3, private memory commands, tiến độ realtime,
  rate limit và phản hồi dài.

Tệp upload tạm được xóa ngay sau lượt document và không thuộc memory.
Forget không xóa lịch sử Telegram/provider, backup hoặc usage. Tool logs Sprint 2
chưa có owner được giữ vì không thể xóa an toàn theo user.
Chi tiết và cách chạy thực nghiệm: `docs/huong-dan-nguyen-ly-sprint-3.md`.

## So sánh context offline

```bash
.venv/bin/python scripts/eval_context.py \
  --mode offline --strategies full,window10,sprint3 --repeat 2
```

Kết quả JSON/Markdown được ghi vào `experiments/results/<run-id>/`. Offline
probe đo cơ chế/evidence availability, không đại diện chất lượng hoặc chi phí
model thật.

## Thực nghiệm live đã nghiệm thu

```bash
.venv/bin/python scripts/eval_context.py \
  --mode live --provider router9 --strategies full,window10,sprint3 \
  --repeat 1 --max-calls 600 --stop-after-observed-usd 0 --pacing-seconds 0.2
```

Run cuối `20260916T145221Z` hoàn tất 24/24 case-strategy,
`incomplete=false`: full 8/8, window10 4/8, sprint3 7/8. Hai artifact giữ
trong repo là offline `20260912T105242Z` và live `20260916T145221Z`.

## Kiểm tra chất lượng

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

CI chạy hai lệnh trên với Python 3.11 và 3.12. Toàn bộ test dùng MockLLM và
SQLite in-memory — không cần mạng hay API key.
