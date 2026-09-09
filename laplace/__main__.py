"""Command-line entrypoint for ``python -m laplace``."""

import argparse

from laplace.agent import Agent
from laplace.config import Settings
from laplace.tasks import successful_sample


def main() -> None:
    """Chạy CLI mặc định hoặc trình diễn một nhiệm vụ Agent mẫu."""
    parser = argparse.ArgumentParser(description="Laplace's Demon command line interface")
    parser.add_argument(
        "--sample-agent",
        action="store_true",
        help="run the deterministic sample Agent task",
    )
    parser.add_argument(
        "--chat",
        metavar="MESSAGE",
        help="run one chat turn through the real agent pipeline (LLM + tools + DB)",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="start the Telegram bot (requires LAPLACE_TELEGRAM_BOT_TOKEN)",
    )
    # Giữ tương thích với entrypoint cũ: tham số chưa được hỗ trợ không làm
    # hỏng lệnh mặc định trong giai đoạn bootstrap.
    arguments, _ = parser.parse_known_args()

    if arguments.bot:
        from laplace.bot.runner import main as run_bot_main

        run_bot_main()
        return

    if arguments.chat:
        from laplace.db import init_db
        from laplace.services.chat import handle_message

        init_db()
        # CLI dùng identity dev cố định; identity thật đến từ Telegram.
        reply = handle_message(0, "cli", arguments.chat, progress=print)
        print(reply.text)
        print(f"usage={reply.usage}")
        return

    if arguments.sample_agent:
        result = Agent().run(successful_sample())
        print(f"status={result.status}")
        print(f"completion_reason={result.completion_reason}")
        return

    settings = Settings()
    print(
        f"{settings.app_name} is ready "
        f"(environment={settings.environment}, log_level={settings.log_level})."
    )


if __name__ == "__main__":
    main()
