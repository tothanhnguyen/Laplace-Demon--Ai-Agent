"""Application settings loaded from environment variables or a local file."""

from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
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

    # Tool runtime Sprint 4: bounded defaults; secrets remain empty.
    tavily_api_key: str = ""
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    web_max_results: int = Field(default=5, ge=1, le=10)
    web_max_chunks: int = Field(default=3, ge=1, le=5)
    document_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=10 * 1024 * 1024)
    document_max_units: int = Field(default=200, ge=1, le=500)
    document_unit_max_chars: int = Field(default=4_000, ge=500, le=20_000)
    document_full_summary_max_chars: int = Field(default=4_000, ge=1_000, le=100_000)
    document_parse_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    document_parser_memory_mb: int = Field(default=256, ge=64, le=1_024)
    document_max_expanded_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        le=200 * 1024 * 1024,
    )
    document_max_zip_entries: int = Field(default=1_000, ge=1, le=10_000)
    timezone: str = "Asia/Ho_Chi_Minh"
    reminder_scan_interval_seconds: int = Field(default=1, ge=1, le=60)
    reminder_misfire_grace_seconds: int = Field(default=3_600, ge=1, le=86_400)
    reminder_max_horizon_days: int = Field(default=365, ge=1, le=3_650)
    reminder_max_attempts: int = Field(default=3, ge=1, le=10)
    reminder_send_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    reminder_batch_size: int = Field(default=25, ge=1, le=100)
    reminder_max_concurrency: int = Field(default=5, ge=1, le=20)
    approval_timeout_seconds: int = Field(default=900, ge=1, le=900)

    # One cumulative execution budget, including every physical model attempt.
    max_steps: int = Field(default=5, ge=1, le=20)
    max_seconds: float = Field(default=120, ge=1, le=600, allow_inf_nan=False)
    max_cost_per_task: Decimal = Field(default=Decimal("0.10"), ge=0, le=10)
    llm_max_completion_tokens: int = Field(default=2_048, ge=1, le=8_192)
    llm_timeout_seconds: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    max_repeated_errors: int = Field(default=3, ge=1, le=5)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA zone") from exc
        return value
