from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.bot import TradingBot
from okx_bot.config import BotConfig
from okx_bot.models import Signal
from okx_bot.state import BotState


class FakeExchange:
    def __init__(self) -> None:
        self.cancel_payload = None

    def load_markets(self) -> None:
        return None

    def market(self, symbol: str) -> dict[str, str]:
        return {"id": symbol.replace("/", "-")}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.2f}"

    def private_post_trade_cancel_algos(self, payload):
        self.cancel_payload = payload
        return {"code": "0", "data": [{"algoId": payload[0]["algoId"], "sCode": "0"}]}


class BotSignalGateTests(unittest.TestCase):
    def test_low_confidence_trade_signal_is_blocked(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(
            os.environ, {"SIGNAL_CONFIDENCE_THRESHOLD": "0.80"}, clear=True
        ):
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
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(
            os.environ, {"SIGNAL_CONFIDENCE_THRESHOLD": "0.80"}, clear=True
        ):
            config = BotConfig.from_env()
        bot_like = object.__new__(TradingBot)
        bot_like.config = config
        signal = Signal(
            "buy",
            "Test buy signal.",
            Decimal("100"),
            {"confidence": 0.80},
            Decimal("0.80"),
        )

        gated = TradingBot.apply_signal_confidence_gate(bot_like, signal)

        self.assertEqual(gated.action, "buy")


class BotProtectiveOrderTests(unittest.TestCase):
    def make_bot(self) -> TradingBot:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {}, clear=True):
            config = BotConfig.from_env()
        bot = object.__new__(TradingBot)
        bot.config = config
        bot.exchange = FakeExchange()
        bot.state = BotState()
        return bot

    def test_build_protective_oco_payload(self) -> None:
        bot = self.make_bot()

        payload = bot.build_protective_oco_payload(Decimal("0.01"), Decimal("100"), "BTC/USDT")

        self.assertEqual(payload["instId"], "BTC-USDT")
        self.assertEqual(payload["tdMode"], "cash")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["ordType"], "oco")
        self.assertEqual(payload["sz"], "0.01")
        self.assertEqual(payload["tpTriggerPx"], "104.00")
        self.assertEqual(payload["tpOrdPx"], "-1")
        self.assertEqual(payload["slTriggerPx"], "98.00")
        self.assertEqual(payload["slOrdPx"], "-1")
        self.assertEqual(payload["tpTriggerPxType"], "last")
        self.assertEqual(payload["slTriggerPxType"], "last")

    def test_cancel_protective_order_uses_algo_id(self) -> None:
        bot = self.make_bot()
        bot.state.set_protective_order("BTC/USDT", "12345", "client123")

        bot.cancel_protective_order_if_present("BTC/USDT")

        self.assertEqual(bot.exchange.cancel_payload, [{"algoId": "12345", "instId": "BTC-USDT"}])
        self.assertIsNone(bot.state.get_protective_algo_id("BTC/USDT"))


if __name__ == "__main__":
    unittest.main()
