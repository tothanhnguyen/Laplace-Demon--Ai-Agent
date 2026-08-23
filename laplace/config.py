"""Application settings loaded from environment variables or a local file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Minimal settings required by the project bootstrap."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_prefix="LAPLACE_",
        extra="ignore",
    )

    app_name: str = "Laplace's Demon"
    environment: str = "development"
    log_level: str = "INFO"
