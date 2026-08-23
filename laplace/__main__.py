"""Command-line entrypoint for ``python -m laplace``."""

from laplace.config import Settings


def main() -> None:
    """Print a small readiness message for the current environment."""
    settings = Settings()
    print(
        f"{settings.app_name} is ready "
        f"(environment={settings.environment}, log_level={settings.log_level})."
    )


if __name__ == "__main__":
    main()
