# Laplace's Demon

AI Agent cá nhân giao tiếp qua Telegram, phát triển theo từng sprint.
Sprint 3 đã hoàn tất: context ba tầng có ngân sách, task card theo bước,
rolling session memory, quản lý bộ nhớ theo user và thực nghiệm live schema v2.

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
memory trong private chat), `/forget` và `/forget confirm` (xóa memory có
liên kết nhưng giữ usage), `/cancel` (cooperative cancellation). Bot nhận tệp
đính kèm, hiển thị task card theo bước, chia phản hồi quá 4096 ký tự và giới
hạn 5 yêu cầu/phút mỗi user.

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

## Kiến trúc (Sprint 3)

- `laplace/context.py` — context ba tầng, char budget, bounded tool observation,
  task card và rolling-summary schema.
- `laplace/services/chat.py` — shared agent runtime; tối đa 5 tool executions;
  ghi task/step/tool/trace ownership và compact history theo watermark.
- `laplace/services/memory.py` — một worker process-local mỗi user,
  cooperative cancellation, memory snapshot và atomic forget.
- `laplace/llm/` — provider OpenAI-compatible, retry 429, usage/cost và mock offline.
- `laplace/tools/` — registry + Pydantic params; `read_file` là tool mẫu.
- `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` — SQLAlchemy/SQLite,
  migration additive, FK checks và child-first deletion.
- `laplace/bot/` — aiogram 3, private memory commands, tiến độ realtime,
  rate limit và phản hồi dài.

Forget không xóa file upload, Telegram/provider retention, backup hoặc usage.
Tool logs Sprint 2 chưa có owner được giữ vì không thể xóa an toàn theo user.
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
