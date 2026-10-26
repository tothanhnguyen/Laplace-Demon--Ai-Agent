"""Command-line entrypoint for ``python -m laplace``."""

import argparse
import logging

from laplace.agent import Agent
from laplace.config import Settings, configured_secrets
from laplace.logging import redact_text, setup_logging
from laplace.tasks import successful_sample


def main() -> None:
    """Chạy CLI mặc định hoặc trình diễn một nhiệm vụ Agent mẫu."""
    # Install before importing bot/providers; include configured keys without
    # ever displaying configuration or passing them to operational events.
    setup_logging()
    settings = Settings()
    secrets = configured_secrets(settings)
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
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="serve the read-only observation pages on 127.0.0.1 (Sprint 7)",
    )
    parser.add_argument("--port", type=int, default=8787, help="viewer port")
    parser.add_argument("--db", default=None, help="viewer database URL override")
    # Giữ tương thích với entrypoint cũ: tham số chưa được hỗ trợ không làm
    # hỏng lệnh mặc định trong giai đoạn bootstrap.
    arguments, _ = parser.parse_known_args()

    if arguments.bot:
        from laplace.bot.runner import main as run_bot_main

        run_bot_main()
        return

    if arguments.viewer:
        from laplace.viewer.__main__ import serve

        serve(port=arguments.port, database_url=arguments.db)
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
