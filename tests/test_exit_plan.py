from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig
from okx_bot.exit_plan import build_exit_plan
from okx_bot.models import Candle, Signal


def candle(index: int, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        timestamp=index,
        open=price - Decimal("0.2"),
        high=price + Decimal("0.8"),
        low=price - Decimal("0.8"),
        close=price,
        volume=Decimal("10"),
    )


def make_config(extra_env: dict[str, str] | None = None) -> BotConfig:
    env = {
        "SYMBOL_STOP_LOSS_PCTS": "ETH:0.015",
        "SYMBOL_TAKE_PROFIT_PCTS": "ETH:0.06",
        "DYNAMIC_EXIT_ENABLED": "true",
        "DYNAMIC_EXIT_MIN_STOP_PCT": "0.012",
        "DYNAMIC_EXIT_MAX_STOP_PCT": "0.030",
        "DYNAMIC_EXIT_STRONG_CONFIDENCE": "0.70",
        "EXTERNAL_CONTEXT_ENABLED": "false",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()
        config.validate()
        return config


class ExitPlanTests(unittest.TestCase):
    def test_strong_trend_uses_strong_reward_risk(self) -> None:
        config = make_config()
        candles = [candle(index, str(100 + index)) for index in range(30)]
        signal = Signal(
            "buy",
            "strong",
            Decimal("130"),
            {"higher_timeframe_bullish_alignment": 1.0},
            Decimal("0.72"),
        )

        plan = build_exit_plan(config, "ETH/USDT:USDT", Decimal("130"), "long", signal, candles)

        self.assertTrue(plan.dynamic)
        self.assertEqual(plan.reward_risk, Decimal("4.0"))
        self.assertGreaterEqual(plan.stop_loss_pct, config.dynamic_exit_min_stop_pct)
        self.assertLessEqual(plan.stop_loss_pct, config.dynamic_exit_max_stop_pct)
        self.assertEqual(plan.take_profit_pct, plan.stop_loss_pct * Decimal("4.0"))

    def test_weaker_trend_uses_symbol_baseline_exit(self) -> None:
        config = make_config()
        candles = [candle(index, str(100 + (index % 3))) for index in range(30)]
        signal = Signal("buy", "mixed", Decimal("101"), {}, Decimal("0.68"))

        plan = build_exit_plan(config, "ETH/USDT:USDT", Decimal("101"), "long", signal, candles)

        self.assertFalse(plan.dynamic)
        self.assertEqual(plan.stop_loss_pct, Decimal("0.015"))
        self.assertEqual(plan.take_profit_pct, Decimal("0.06"))

    def test_dynamic_exit_is_limited_to_configured_symbols(self) -> None:
        config = make_config({"DYNAMIC_EXIT_SYMBOLS": "ETH"})
        candles = [candle(index, str(100 + index)) for index in range(30)]
        signal = Signal(
            "buy",
            "strong",
            Decimal("130"),
            {"higher_timeframe_bullish_alignment": 1.0},
            Decimal("0.72"),
        )

        plan = build_exit_plan(config, "BTC/USDT:USDT", Decimal("130"), "long", signal, candles)

        self.assertFalse(plan.dynamic)
        self.assertEqual(plan.stop_loss_pct, Decimal("0.02"))
        self.assertEqual(plan.take_profit_pct, Decimal("0.04"))


if __name__ == "__main__":
    unittest.main()
