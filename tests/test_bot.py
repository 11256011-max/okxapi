from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.bot import TradingBot
from okx_bot.config import BotConfig
from okx_bot.models import Signal


class BotSignalGateTests(unittest.TestCase):
    def test_low_confidence_trade_signal_is_blocked(self) -> None:
        with patch.dict(os.environ, {"SIGNAL_CONFIDENCE_THRESHOLD": "0.90"}, clear=True):
            config = BotConfig.from_env()
        bot_like = object.__new__(TradingBot)
        bot_like.config = config
        signal = Signal(
            "buy",
            "Test buy signal.",
            Decimal("100"),
            {"confidence": 0.75},
            Decimal("0.75"),
        )

        gated = TradingBot.apply_signal_confidence_gate(bot_like, signal)

        self.assertEqual(gated.action, "hold")
        self.assertIn("below threshold", gated.reason)

    def test_high_confidence_trade_signal_is_allowed(self) -> None:
        with patch.dict(os.environ, {"SIGNAL_CONFIDENCE_THRESHOLD": "0.90"}, clear=True):
            config = BotConfig.from_env()
        bot_like = object.__new__(TradingBot)
        bot_like.config = config
        signal = Signal(
            "buy",
            "Test buy signal.",
            Decimal("100"),
            {"confidence": 0.95},
            Decimal("0.95"),
        )

        gated = TradingBot.apply_signal_confidence_gate(bot_like, signal)

        self.assertEqual(gated.action, "buy")


if __name__ == "__main__":
    unittest.main()
