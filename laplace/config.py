"""Application settings loaded from environment variables or a local file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_prefix="LAPLACE_",
        extra="ignore",
    )

    app_name: str = "Laplace's Demon"
    environment: str = "development"
    log_level: str = "INFO"

    # LLM: chọn nhà cung cấp bằng cấu hình, không sửa mã nguồn (Sprint 2).
    # "mock" chạy offline không cần key; các preset thật xem laplace/llm/presets.py.
    llm_provider: str = "mock"
    # Override model chung cho mọi provider; rỗng thì dùng model mặc định của preset.
    llm_model: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    groq_api_key: str = ""
    bai_api_key: str = ""
    router9_api_key: str = ""

    # Lưu trữ: SQLite theo thiết kế Sprint 1; đường dẫn tương đối tính từ repo.
    database_url: str = "sqlite:///laplace.db"

    # Telegram: token lấy từ @BotFather; rỗng thì lệnh --bot báo lỗi hướng dẫn.
    telegram_bot_token: str = ""

    # Context Sprint 3: ngân sách ký tự cho messages do ứng dụng dựng.
    context_max_chars: int = 12_000
