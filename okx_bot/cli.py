from __future__ import annotations

import argparse
import json
import logging
import re
import time

from ccxt.base.errors import AuthenticationError, PermissionDenied

from .backtest import BacktestRunner
from .bot import TradingBot
from .config import BotConfig, ConfigError

OKX_AUTH_ERRORS = (AuthenticationError, PermissionDenied)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OKX trading bot")
    parser.add_argument(
        "command",
        choices=("once", "loop", "balance", "backtest", "ui"),
        help="Run one strategy pass, run forever, show balances, run a public-OHLCV backtest, or open the local UI.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Backtest lookback window in days.",
    )
    parser.add_argument(
        "--trades",
        type=int,
        default=100,
        help="Maximum completed backtest trades to report.",
    )
    parser.add_argument(
        "--csv",
        default="",
        help="Optional path to write reported backtest trades as CSV.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the local UI.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port for the local UI.",
    )
    return parser


def concise_okx_error_message(exc: Exception) -> str:
    message = str(exc)
    match = re.search(r"(\{.*\})", message)
    if not match:
        return message
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return message
    okx_message = payload.get("msg")
    okx_code = payload.get("code")
    if okx_message and okx_code:
        return f"OKX code {okx_code}: {okx_message}"
    if okx_message:
        return str(okx_message)
    return message


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = BotConfig.from_env()
        config.validate(
            require_private=args.command == "balance",
            require_order_submission=args.command not in {"backtest", "ui"},
        )

        if args.command == "backtest":
            BacktestRunner(config).run(days=args.days, max_trades=args.trades, csv_path=args.csv or None)
            return 0

        if args.command == "ui":
            from .ui import run_ui

            run_ui(config, host=args.host, port=args.port)
            return 0

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
    except ValueError as exc:
        logging.error("Input error: %s", exc)
        return 2
    except OKX_AUTH_ERRORS as exc:
        logging.error("OKX API permission/authentication error: %s", concise_okx_error_message(exc))
        logging.error(
            "If this mentions IP whitelist, add this machine's current public IP to the OKX API key whitelist. "
            "If it mentions environment mismatch, use demo keys with OKX_SIMULATED_TRADING=true or live keys with it=false."
        )
        return 2
    except Exception:
        logging.exception("Bot stopped because of an unexpected error.")
        return 1
