from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.bot import TradingBot
from okx_bot.config import BotConfig
from okx_bot.external_context import ContextSnapshot
from okx_bot.models import Signal
from okx_bot.risk import RiskError
from okx_bot.state import BotState


class FakeExchange:
    def __init__(self, equity: str = "10000") -> None:
        self.equity = equity
        self.orders = []
        self.leverage_calls = []

    def load_markets(self) -> None:
        return None

    def market(self, symbol: str) -> dict[str, str]:
        return {"id": symbol.replace("/", "-").replace(":", "-"), "contractSize": "1"}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    def fetch_balance(self, params=None):
        return {
            "info": {"data": [{"totalEq": self.equity}]},
            "total": {"USDT": self.equity},
            "free": {"USDT": self.equity},
        }

    def set_leverage(self, leverage: int, symbol: str, params=None):
        self.leverage_calls.append((leverage, symbol, params or {}))
        return {"code": "0"}

    def create_order(self, symbol: str, order_type: str, side: str, amount: float, price, params=None):
        order = {
            "id": f"order-{len(self.orders) + 1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "average": None,
            "params": params or {},
        }
        self.orders.append(order)
        return order


class FakeExternalContext:
    def __init__(self, snapshot: ContextSnapshot) -> None:
        self.snapshot = snapshot

    def evaluate(self, symbol: str) -> ContextSnapshot:
        return self.snapshot


def make_config(extra_env: dict[str, str] | None = None) -> BotConfig:
    env = {
        "MARKET_TYPE": "swap",
        "SYMBOL": "BTC/USDT",
        "ORDER_QUOTE_AMOUNT": "1000",
        "MAX_QUOTE_PER_ORDER": "1000",
        "STOP_LOSS_PCT": "0.02",
        "TAKE_PROFIT_PCT": "0.04",
        "RISK_PER_TRADE_PCT": "0.01",
        "DAILY_MAX_LOSS_PCT": "0.06",
        "MAX_LEVERAGE": "10",
        "EXTERNAL_CONTEXT_ENABLED": "false",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()
        config.validate()
        return config


def make_bot(extra_env: dict[str, str] | None = None, equity: str = "10000") -> TradingBot:
    config = make_config(extra_env)
    bot = object.__new__(TradingBot)
    bot.config = config
    bot.exchange = FakeExchange(equity)
    bot.external_context = None
    bot.state = BotState(default_symbol=config.symbols[0])
    return bot


class BotSignalGateTests(unittest.TestCase):
    def test_low_confidence_trade_signal_is_blocked(self) -> None:
        config = make_config({"SIGNAL_CONFIDENCE_THRESHOLD": "0.80"})
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
        config = make_config({"SIGNAL_CONFIDENCE_THRESHOLD": "0.80"})
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


class BotExternalContextTests(unittest.TestCase):
    def test_external_context_increases_aligned_buy_confidence(self) -> None:
        bot = make_bot({"EXTERNAL_CONTEXT_ENABLED": "true"})
        bot.external_context = FakeExternalContext(
            ContextSnapshot(combined_score=Decimal("0.6"), newsapi_score=Decimal("0.6"), sources_used=1)
        )
        signal = Signal("buy", "Strategy buy.", Decimal("100"), {}, Decimal("0.70"))

        adjusted = bot.apply_external_context_filter("BTC/USDT:USDT", signal)

        self.assertEqual(adjusted.action, "buy")
        self.assertGreater(adjusted.confidence, signal.confidence)
        self.assertEqual(adjusted.indicators["newsapi_score"], 0.6)

    def test_external_context_blocks_contrary_buy_signal(self) -> None:
        bot = make_bot({"EXTERNAL_CONTEXT_ENABLED": "true", "EXTERNAL_CONTEXT_MIN_SUPPORT": "-0.30"})
        bot.external_context = FakeExternalContext(
            ContextSnapshot(combined_score=Decimal("-0.8"), gdelt_score=Decimal("-0.8"), sources_used=1)
        )
        signal = Signal("buy", "Strategy buy.", Decimal("100"), {}, Decimal("0.90"))

        adjusted = bot.apply_external_context_filter("BTC/USDT:USDT", signal)

        self.assertEqual(adjusted.action, "hold")
        self.assertIn("blocked by external context", adjusted.reason)

    def test_external_context_negative_score_supports_sell_signal(self) -> None:
        bot = make_bot({"EXTERNAL_CONTEXT_ENABLED": "true"})
        bot.external_context = FakeExternalContext(
            ContextSnapshot(combined_score=Decimal("-0.4"), fear_greed_score=Decimal("-0.4"), sources_used=1)
        )
        signal = Signal("sell", "Strategy sell.", Decimal("100"), {}, Decimal("0.70"))

        adjusted = bot.apply_external_context_filter("BTC/USDT:USDT", signal)

        self.assertEqual(adjusted.action, "sell")
        self.assertGreater(adjusted.confidence, signal.confidence)

    def test_external_context_is_logged_for_hold_signal(self) -> None:
        bot = make_bot({"EXTERNAL_CONTEXT_ENABLED": "true"})
        bot.external_context = FakeExternalContext(
            ContextSnapshot(combined_score=Decimal("0.2"), gdelt_score=Decimal("0.2"), sources_used=1)
        )
        signal = Signal("hold", "No setup.", Decimal("100"), {}, Decimal("0"))

        adjusted = bot.apply_external_context_filter("BTC/USDT:USDT", signal)

        self.assertEqual(adjusted.action, "hold")
        self.assertEqual(adjusted.indicators["external_context_score"], 0.2)
        self.assertIn("External context score", adjusted.reason)


class BotSwapRiskTests(unittest.TestCase):
    def test_build_swap_position_plan_uses_one_percent_risk(self) -> None:
        bot = make_bot()

        plan = bot.build_swap_position_plan("BTC/USDT:USDT", Decimal("100"))

        self.assertEqual(plan.equity, Decimal("10000"))
        self.assertEqual(plan.risk_amount, Decimal("100.00"))
        self.assertEqual(plan.margin_budget, Decimal("1000"))
        self.assertEqual(plan.notional, Decimal("5000"))
        self.assertEqual(plan.leverage, 5)
        self.assertEqual(plan.amount_contracts, Decimal("50"))

    def test_short_exit_prices_reverse_take_profit_and_stop_loss(self) -> None:
        bot = make_bot()

        take_profit, stop_loss = bot.exit_prices(Decimal("100"), "short")

        self.assertEqual(take_profit, Decimal("96.00"))
        self.assertEqual(stop_loss, Decimal("102.00"))

    def test_swap_order_attaches_short_take_profit_and_stop_loss(self) -> None:
        bot = make_bot({"DRY_RUN": "false", "OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass"})

        order = bot.create_swap_market_order_with_tp_sl(
            "BTC/USDT:USDT",
            Decimal("2"),
            Decimal("100"),
            order_side="sell",
            position_side="short",
        )

        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["params"]["tdMode"], "isolated")
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 96.0)
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 102.0)

    def test_daily_loss_limit_blocks_new_positions(self) -> None:
        bot = make_bot()
        bot.state.daily_realized_pnl = Decimal("-600")

        with self.assertRaises(RiskError):
            bot.assert_daily_loss_limit_not_hit(Decimal("10000"))

    def test_buy_signal_closes_existing_short(self) -> None:
        bot = make_bot()
        symbol = "BTC/USDT:USDT"
        bot.state.record_trade("sell", Decimal("2"), Decimal("100"), Decimal("200"), "dry-run", symbol=symbol, position_side="short")
        signal = Signal("buy", "Close short.", Decimal("95"), {}, Decimal("1"))

        bot.buy(symbol, signal)

        self.assertIsNone(bot.state.get_position_side(symbol))
        self.assertEqual(bot.state.daily_realized_pnl, Decimal("10"))

    def test_sell_signal_opens_short_when_flat(self) -> None:
        bot = make_bot()
        symbol = "BTC/USDT:USDT"
        signal = Signal("sell", "Open short.", Decimal("100"), {}, Decimal("1"))

        bot.sell(symbol, signal)

        self.assertEqual(bot.state.get_position_side(symbol), "short")
        self.assertEqual(bot.state.get_position_base(symbol), Decimal("50"))


if __name__ == "__main__":
    unittest.main()
