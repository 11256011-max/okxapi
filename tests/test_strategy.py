from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig
from okx_bot.models import Candle
from okx_bot.strategy import SmcStrategy, create_strategy


def candle(index: int, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        timestamp=index,
        open=price - Decimal("0.2"),
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        volume=Decimal("1"),
    )


class StrategyTests(unittest.TestCase):
    def smc_config(self) -> BotConfig:
        env = {
            "STRATEGY": "smc",
            "CANDLE_LIMIT": "25",
            "SMC_SWING_LOOKBACK": "2",
            "SMC_ZONE_LOOKBACK": "10",
            "SMC_MIN_DISPLACEMENT_PCT": "0.001",
            "SMC_REQUIRE_FVG": "false",
        }
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
            return BotConfig.from_env()

    def test_create_strategy_selects_smc(self) -> None:
        config = self.smc_config()
        self.assertIsInstance(create_strategy(config), SmcStrategy)

    def test_smc_generates_buy_on_bullish_break_of_structure(self) -> None:
        config = self.smc_config()
        strategy = SmcStrategy(config)
        closes = [
            "100",
            "102",
            "104",
            "102",
            "100",
            "98",
            "96",
            "98",
            "100",
            "104",
            "107",
            "104",
            "102",
            "100",
            "99",
            "100",
            "102",
            "104",
            "106",
            "107",
            "110",
        ]
        candles = [candle(index, close) for index, close in enumerate(closes)]

        signal = strategy.generate(candles)

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.indicators["bullish_bos"], 1.0)
        self.assertGreaterEqual(signal.confidence, Decimal("0.90"))


if __name__ == "__main__":
    unittest.main()
