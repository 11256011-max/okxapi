from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig
from okx_bot.models import Candle
from okx_bot.strategy import CombinedMarketStructureStrategy, create_strategy


def candle(index: int, open_: str, high: str, low: str, close: str, volume: str = "10") -> Candle:
    return Candle(
        timestamp=index,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def config(extra_env: dict[str, str] | None = None) -> BotConfig:
    env = {
        "STRATEGY": "combined",
        "CANDLE_LIMIT": "100",
        "COMBINED_STRUCTURE_LOOKBACK": "30",
        "COMBINED_ORDER_FLOW_LOOKBACK": "20",
        "COMBINED_AVWAP_LOOKBACK": "40",
        "COMBINED_VOLUME_PROFILE_LOOKBACK": "40",
        "COMBINED_MIN_SCORE": "0.80",
        "COMBINED_MIN_EDGE": "0.10",
        "EXTERNAL_CONTEXT_ENABLED": "false",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        bot_config = BotConfig.from_env()
        bot_config.validate()
        return bot_config


def range_candles(count: int = 80) -> list[Candle]:
    candles: list[Candle] = []
    for index in range(count):
        base = Decimal("100") + (Decimal(index % 8) * Decimal("0.25"))
        open_ = base - Decimal("0.10")
        close = base + Decimal("0.05")
        candles.append(
            candle(
                index,
                str(open_),
                str(base + Decimal("0.45")),
                str(base - Decimal("0.45")),
                str(close),
                "10",
            )
        )
    return candles


class StrategyTests(unittest.TestCase):
    def test_create_strategy_selects_combined(self) -> None:
        self.assertIsInstance(create_strategy(config()), CombinedMarketStructureStrategy)

    def test_legacy_smc_name_maps_to_combined(self) -> None:
        self.assertEqual(config({"STRATEGY": "smc"}).strategy, "combined")

    def test_combined_generates_buy_when_all_modules_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        candles = range_candles()
        candles.append(candle(80, "100.2", "105.2", "98.4", "104.7", "35"))

        signal = strategy.generate(candles)

        self.assertEqual(signal.action, "buy")
        self.assertGreaterEqual(signal.confidence, Decimal("0.80"))
        self.assertGreater(signal.indicators["bullish_score"], signal.indicators["bearish_score"])
        self.assertGreaterEqual(signal.indicators["liquidity_sweep_bullish_score"], 1.0)
        self.assertGreaterEqual(signal.indicators["order_flow_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["anchored_vwap_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["volume_profile_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["smc_bullish_score"], 0.5)

    def test_combined_generates_sell_when_all_modules_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        candles = range_candles()
        candles.append(candle(80, "101.6", "103.9", "96.2", "97.0", "35"))

        signal = strategy.generate(candles)

        self.assertEqual(signal.action, "sell")
        self.assertGreaterEqual(signal.confidence, Decimal("0.80"))
        self.assertGreater(signal.indicators["bearish_score"], signal.indicators["bullish_score"])
        self.assertGreaterEqual(signal.indicators["liquidity_sweep_bearish_score"], 1.0)
        self.assertGreaterEqual(signal.indicators["order_flow_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["anchored_vwap_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["volume_profile_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["smc_bearish_score"], 0.5)

    def test_combined_holds_when_composite_score_is_too_low(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        signal = strategy.generate(range_candles())

        self.assertEqual(signal.action, "hold")
        self.assertLess(signal.confidence, Decimal("0.80"))


if __name__ == "__main__":
    unittest.main()
