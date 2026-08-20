# Laplace's Demon

Đây là bản khởi tạo mới để phát triển lại Laplace's Demon theo từng module nhỏ.
Sprint hiện tại chỉ dựng nền dự án (S1-07), chưa gồm database, agent, LLM, tool,
Telegram hoặc execution trace.

## Yêu cầu

- Python 3.11 trở lên

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

## Chạy dự án

```bash
python -m laplace
```

Kết quả mặc định:

```text
Laplace's Demon is ready (environment=development, log_level=INFO).
```

## Cấu hình

Các biến đều có tiền tố `LAPLACE_`:

| Biến | Mặc định | Mục đích |
| --- | --- | --- |
| `LAPLACE_APP_NAME` | `Laplace's Demon` | Tên ứng dụng |
| `LAPLACE_ENVIRONMENT` | `development` | Môi trường chạy |
| `LAPLACE_LOG_LEVEL` | `INFO` | Mức log dự kiến |

## Kiểm tra chất lượng

```bash
ruff check .
pytest
```

CI chạy hai lệnh trên với Python 3.11 và 3.12.
# Laplace-Demon---Ai-Agent
