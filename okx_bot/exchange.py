from __future__ import annotations

from typing import Any

import ccxt

from .config import BotConfig


def create_exchange(config: BotConfig) -> Any:
    exchange_config: dict[str, Any] = {
        "apiKey": config.api_key,
        "secret": config.secret_key,
        "password": config.passphrase,
        "enableRateLimit": True,
        "options": {
            "defaultType": config.market_type,
        },
    }

    exchange = ccxt.okx(exchange_config)

    if config.okx_simulated_trading:
        # CCXT requires sandbox mode to be enabled before any exchange request.
        exchange.set_sandbox_mode(True)
        exchange.headers = {
            **(exchange.headers or {}),
            "x-simulated-trading": "1",
        }

    return exchange

