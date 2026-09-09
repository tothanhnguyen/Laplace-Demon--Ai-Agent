"""Bootstrap tests for settings and the module entrypoint."""

import os
import subprocess
import sys

from laplace.config import Settings

_SETTING_KEYS = (
    "LAPLACE_APP_NAME",
    "LAPLACE_ENVIRONMENT",
    "LAPLACE_LOG_LEVEL",
)


def test_settings_use_defaults(monkeypatch) -> None:
    for key in _SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_name == "Laplace's Demon"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"


def test_settings_read_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LAPLACE_APP_NAME", "Laplace Test")
    monkeypatch.setenv("LAPLACE_ENVIRONMENT", "test")
    monkeypatch.setenv("LAPLACE_LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)

    assert settings.app_name == "Laplace Test"
    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"


def test_module_cli_smoke() -> None:
    env = os.environ.copy()
    env.update(
        {
            "LAPLACE_APP_NAME": "Laplace CLI",
            "LAPLACE_ENVIRONMENT": "test",
            "LAPLACE_LOG_LEVEL": "WARNING",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "laplace"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout.strip() == (
        "Laplace CLI is ready (environment=test, log_level=WARNING)."
    )


def test_sample_agent_cli_explains_stop_reason() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "laplace", "--sample-agent"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "status=completed" in result.stdout
    assert "completion_reason=" in result.stdout
