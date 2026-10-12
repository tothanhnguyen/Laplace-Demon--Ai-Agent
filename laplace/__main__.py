"""Command-line entrypoint for ``python -m laplace``."""

import argparse
import logging

from laplace.agent import Agent
from laplace.config import Settings
from laplace.logging import redact_text, setup_logging
from laplace.tasks import successful_sample


def main() -> None:
    """Chạy CLI mặc định hoặc trình diễn một nhiệm vụ Agent mẫu."""
    # Install before importing bot/providers; include configured keys without
    # ever displaying configuration or passing them to operational events.
    setup_logging()
    settings = Settings()
    secrets = (
        settings.gemini_api_key,
        settings.openai_api_key,
        settings.groq_api_key,
        settings.bai_api_key,
        settings.router9_api_key,
        settings.telegram_bot_token,
        settings.tavily_api_key,
    )
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    setup_logging(secrets=secrets, level=level)
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
        reply = handle_message(
            0,
            "cli",
            arguments.chat,
            progress=print,
            transport="cli",
            is_private_chat=True,
        )
        print(reply.text)
        usage = reply.usage
        print(
            f"usage: logical_calls={usage['llm_calls']}, "
            f"physical_attempts={usage['physical_attempts']}, "
            f"known_tokens={usage['total_tokens']}, "
            f"known_cost_usd={usage['known_cost_usd']:.6f}, "
            f"budget_charge_usd={usage['budget_charge_usd']:.6f}, "
            f"unknown_attempts={usage['unknown_attempts']}"
        )
        return

    if arguments.sample_agent:
        result = Agent().run(successful_sample())
        print(f"status={result.status}")
        print(f"completion_reason={result.completion_reason}")
        return

    print(redact_text(
        f"{settings.app_name} is ready "
        f"(environment={settings.environment}, log_level={settings.log_level}).",
        secrets=secrets,
    ))


if __name__ == "__main__":
    main()
