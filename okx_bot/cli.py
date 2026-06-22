from __future__ import annotations

import argparse
import logging
import time

from .bot import TradingBot
from .config import BotConfig, ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OKX trading bot")
    parser.add_argument(
        "command",
        choices=("once", "loop", "balance"),
        help="Run one strategy pass, run forever, or show balances.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = BotConfig.from_env()
        config.validate(require_private=args.command == "balance")
        bot = TradingBot(config)

        if args.command == "balance":
            bot.print_balance()
            return 0

        if args.command == "once":
            bot.run_once()
            return 0

        while True:
            bot.run_once()
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        return 130
    except ConfigError as exc:
        logging.error("Config error: %s", exc)
        return 2
    except Exception:
        logging.exception("Bot stopped because of an unexpected error.")
        return 1
