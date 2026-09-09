# Laplace's Demon

AI Agent cá nhân giao tiếp qua Telegram, phát triển theo từng sprint.
Trạng thái hiện tại (hết Sprint 2): bot Telegram nói chuyện bằng mô hình ngôn
ngữ thật, theo dõi chi phí từng lời gọi, đổi nhà cung cấp bằng cấu hình.

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
| `LAPLACE_LLM_PROVIDER` | `mock` | Nhà cung cấp LLM: `mock`, `gemini`, `openai`, `groq` |
| `LAPLACE_LLM_MODEL` | (trống) | Override model; trống thì dùng mặc định của provider |
| `LAPLACE_GEMINI_API_KEY` | (trống) | Key Gemini — lấy tại aistudio.google.com/apikey (free tier) |
| `LAPLACE_OPENAI_API_KEY` | (trống) | Key OpenAI |
| `LAPLACE_GROQ_API_KEY` | (trống) | Key Groq |
| `LAPLACE_DATABASE_URL` | `sqlite:///laplace.db` | Nơi lưu phiên, tin nhắn, log LLM/tool/trace |
| `LAPLACE_TELEGRAM_BOT_TOKEN` | (trống) | Token bot từ @BotFather |

Đổi nhà cung cấp mô hình chỉ cần sửa `.env`, không sửa mã nguồn. Thiếu key thì
chương trình báo đúng trang lấy key của hãng tương ứng.

## Chạy bot Telegram

1. Tạo bot với [@BotFather](https://t.me/BotFather), copy token.
2. Đặt `LAPLACE_TELEGRAM_BOT_TOKEN` và key LLM vào `.env`.
3. Chạy:

```bash
python -m laplace --bot
```

Lệnh bot: `/start`, `/help`, `/status` (usage + chi phí của bạn), `/cancel`
(hủy lượt đang xử lý). Bot nhận tệp đính kèm (lưu lại, tóm tắt nội dung thuộc
sprint sau), hiển thị tiến độ từng bước, chia phản hồi dài quá 4096 ký tự và
giới hạn 5 yêu cầu/phút mỗi người dùng.

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

## Kiến trúc (hết Sprint 2)

- `laplace/llm/` — provider tương thích OpenAI (Gemini/OpenAI/Groq qua
  `base_url`), retry khi 429, tính cost theo bảng giá, mock offline cho test.
- `laplace/tools/` — registry tool, tham số validate bằng Pydantic trước khi
  chạy; tool mẫu `read_file`.
- `laplace/prompts.py` — system prompt v1; LLM trả JSON theo schema
  `AgentAction`, sai schema thì được yêu cầu tự sửa.
- `laplace/services/chat.py` — vòng lặp một lượt chat (tối đa 5 bước tool),
  ghi `llm_calls`, `tool_calls`, `traces`, messages vào DB.
- `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` — SQLAlchemy 2.0 +
  SQLite: users, conversations, messages, tasks, steps, tool_calls,
  llm_calls, traces.
- `laplace/bot/` — aiogram 3: handlers, middleware identity + rate limit,
  chia tin nhắn dài, tiến độ realtime, runner polling.

## Kiểm tra chất lượng

```bash
ruff check .
pytest
```

CI chạy hai lệnh trên với Python 3.11 và 3.12. Toàn bộ test dùng MockLLM và
SQLite in-memory — không cần mạng hay API key.
