from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.bot import TradingBot
from okx_bot.config import BotConfig
from okx_bot.external_context import ContextSnapshot
from okx_bot.models import Candle, Signal
from okx_bot.risk import RiskError
from okx_bot.state import BotState


class FakeExchange:
    def __init__(self, equity: str = "10000", hedged: bool = False, leverage_max: str = "100") -> None:
        self.equity = equity
        self.hedged = hedged
        self.leverage_max = leverage_max
        self.orders = []
        self.leverage_calls = []
        self.ohlcv_calls = []

    def load_markets(self) -> None:
        return None

    def market(self, symbol: str) -> dict[str, str]:
        return {
            "id": symbol.replace("/", "-").replace(":", "-"),
            "contractSize": "1",
            "limits": {"leverage": {"min": 1, "max": self.leverage_max}},
            "info": {"lever": self.leverage_max},
        }

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    def fetch_balance(self, params=None):
        return {
            "info": {"data": [{"totalEq": self.equity}]},
            "total": {"USDT": self.equity},
            "free": {"USDT": self.equity},
        }

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        self.ohlcv_calls.append((symbol, timeframe, limit))
        return [
            [
                index * 60000,
                100 + index,
                101 + index,
                99 + index,
                100 + index,
                10,
            ]
            for index in range(limit)
        ]

    def fetch_position_mode(self):
        return {"hedged": self.hedged}

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


def candle(index: int, open_: str, high: str, low: str, close: str, volume: str = "10") -> Candle:
    return Candle(
        timestamp=index,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def long_breakout_candles() -> list[Candle]:
    candles = []
    for index in range(25):
        close = Decimal("100") + (Decimal(index) * Decimal("0.2"))
        candles.append(
            candle(
                index,
                str(close - Decimal("0.1")),
                str(close + Decimal("0.5")),
                str(close - Decimal("0.5")),
                str(close),
                "10",
            )
        )
    candles.append(candle(25, "106", "111", "105.5", "110", "20"))
    return candles


def long_no_add_candles() -> list[Candle]:
    candles = []
    for index in range(25):
        close = Decimal("100") + (Decimal(index) * Decimal("0.1"))
        candles.append(
            candle(
                index,
                str(close - Decimal("0.05")),
                "110",
                str(close - Decimal("0.5")),
                str(close),
                "10",
            )
        )
    candles.append(candle(25, "103.8", "104.4", "103.7", "104", "10"))
    return candles


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
        "EXTERNAL_CONTEXT_ENABLED": "false",
        "ADD_POSITION_ENABLED": "true",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()
        config.validate()
        return config


def make_bot(
    extra_env: dict[str, str] | None = None,
    equity: str = "10000",
    hedged: bool = False,
    leverage_max: str = "100",
) -> TradingBot:
    config = make_config(extra_env)
    bot = object.__new__(TradingBot)
    bot.config = config
    bot.exchange = FakeExchange(equity, hedged=hedged, leverage_max=leverage_max)
    bot.external_context = None
    bot.state = BotState(default_symbol=config.symbols[0])
    bot._effective_position_mode = None
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

        gated = TradingBot.apply_signal_confidence_gate(bot_like, "BTC/USDT:USDT", signal)

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

        gated = TradingBot.apply_signal_confidence_gate(bot_like, "BTC/USDT:USDT", signal)

        self.assertEqual(gated.action, "buy")

    def test_symbol_confidence_threshold_can_block_one_market(self) -> None:
        config = make_config({
            "SIGNAL_CONFIDENCE_THRESHOLD": "0.68",
            "LONG_CONFIDENCE_THRESHOLD": "0.68",
            "SYMBOL_CONFIDENCE_THRESHOLDS": "BTC:0.72,ETH:0.68",
        })
        bot_like = object.__new__(TradingBot)
        bot_like.config = config
        signal = Signal(
            "buy",
            "Test buy signal.",
            Decimal("100"),
            {"confidence": 0.70},
            Decimal("0.70"),
        )

        btc_signal = TradingBot.apply_signal_confidence_gate(bot_like, "BTC/USDT:USDT", signal)
        eth_signal = TradingBot.apply_signal_confidence_gate(bot_like, "ETH/USDT:USDT", signal)

        self.assertEqual(btc_signal.action, "hold")
        self.assertEqual(eth_signal.action, "buy")

    def test_long_threshold_can_be_stricter_than_short_threshold(self) -> None:
        config = make_config({
            "SIGNAL_CONFIDENCE_THRESHOLD": "0.68",
            "LONG_CONFIDENCE_THRESHOLD": "0.72",
            "SHORT_CONFIDENCE_THRESHOLD": "0.68",
        })
        bot_like = object.__new__(TradingBot)
        bot_like.config = config
        buy_signal = Signal("buy", "Test buy signal.", Decimal("100"), {"confidence": 0.70}, Decimal("0.70"))
        sell_signal = Signal("sell", "Test sell signal.", Decimal("100"), {"confidence": 0.70}, Decimal("0.70"))

        gated_buy = TradingBot.apply_signal_confidence_gate(bot_like, "ETH/USDT:USDT", buy_signal)
        gated_sell = TradingBot.apply_signal_confidence_gate(bot_like, "ETH/USDT:USDT", sell_signal)

        self.assertEqual(gated_buy.action, "hold")
        self.assertEqual(gated_sell.action, "sell")


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

    def test_external_context_extreme_score_reduces_position_risk(self) -> None:
        bot = make_bot({"EXTERNAL_CONTEXT_ENABLED": "true", "EXTERNAL_CONTEXT_EXTREME_THRESHOLD": "0.75", "EXTERNAL_CONTEXT_RISK_MULTIPLIER": "0.50"})
        bot.external_context = FakeExternalContext(
            ContextSnapshot(combined_score=Decimal("0.80"), fear_greed_score=Decimal("0.80"), sources_used=1)
        )
        signal = Signal("buy", "Strategy buy.", Decimal("100"), {}, Decimal("0.80"))

        adjusted = bot.apply_external_context_filter("BTC/USDT:USDT", signal)

        self.assertEqual(adjusted.action, "buy")
        self.assertEqual(adjusted.indicators["risk_multiplier"], 0.5)

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
    def test_fetch_analysis_candles_uses_entry_and_confirmation_timeframes(self) -> None:
        bot = make_bot({
            "ENTRY_TIMEFRAME": "30m",
            "CONFIRMATION_TIMEFRAMES": "1h,4h",
            "CANDLE_LIMIT": "100",
        })

        candles_by_timeframe = bot.fetch_analysis_candles("BTC/USDT:USDT")

        self.assertEqual(set(candles_by_timeframe), {"30m", "1h", "4h"})
        self.assertEqual([call[1] for call in bot.exchange.ohlcv_calls], ["30m", "1h", "4h"])

    def test_build_swap_position_plan_uses_one_percent_risk(self) -> None:
        bot = make_bot()

        plan = bot.build_swap_position_plan("BTC/USDT:USDT", Decimal("100"))

        self.assertEqual(plan.equity, Decimal("10000"))
        self.assertEqual(plan.risk_amount, Decimal("100.00"))
        self.assertEqual(plan.margin_budget, Decimal("1000"))
        self.assertEqual(plan.notional, Decimal("5000"))
        self.assertEqual(plan.leverage, 5)
        self.assertEqual(plan.amount_contracts, Decimal("50"))

    def test_build_swap_position_plan_caps_leverage_by_market_limit(self) -> None:
        bot = make_bot({"ORDER_QUOTE_AMOUNT": "10", "MAX_QUOTE_PER_ORDER": "10"})

        plan = bot.build_swap_position_plan("BTC/USDT:USDT", Decimal("100"))

        self.assertEqual(plan.risk_amount, Decimal("100.00"))
        self.assertEqual(plan.margin_budget, Decimal("10"))
        self.assertEqual(plan.notional, Decimal("1000"))
        self.assertEqual(plan.leverage, 100)
        self.assertEqual(plan.amount_contracts, Decimal("10"))

    def test_short_exit_prices_reverse_take_profit_and_stop_loss(self) -> None:
        bot = make_bot()

        take_profit, stop_loss = bot.exit_prices(Decimal("100"), "short")

        self.assertEqual(take_profit, Decimal("96.00"))
        self.assertEqual(stop_loss, Decimal("102.00"))

    def test_symbol_exit_prices_override_global_take_profit_and_stop_loss(self) -> None:
        bot = make_bot({
            "SYMBOL_STOP_LOSS_PCTS": "ETH:0.015",
            "SYMBOL_TAKE_PROFIT_PCTS": "ETH:0.06",
        })

        take_profit, stop_loss = bot.exit_prices(Decimal("100"), "long", "ETH/USDT:USDT")

        self.assertEqual(take_profit, Decimal("106.00"))
        self.assertEqual(stop_loss, Decimal("98.500"))

    def test_build_swap_position_plan_uses_symbol_stop_loss(self) -> None:
        bot = make_bot({
            "SYMBOL_STOP_LOSS_PCTS": "ETH:0.015",
        })

        plan = bot.build_swap_position_plan("ETH/USDT:USDT", Decimal("100"))

        self.assertEqual(plan.risk_amount, Decimal("100.00"))
        self.assertEqual(plan.leverage, 7)
        self.assertEqual(plan.amount_contracts, Decimal("66.66666667"))

    def test_signal_risk_multiplier_reduces_position_size(self) -> None:
        bot = make_bot()
        signal = Signal("buy", "Risk reduced.", Decimal("100"), {"risk_multiplier": 0.5}, Decimal("1"))

        bot.buy("BTC/USDT:USDT", signal)

        self.assertEqual(bot.state.get_position_base("BTC/USDT:USDT"), Decimal("25"))

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
        self.assertEqual(order["params"]["positionSide"], "net")
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 96.0)
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 102.0)

    def test_swap_order_attaches_symbol_take_profit_and_stop_loss(self) -> None:
        bot = make_bot({
            "DRY_RUN": "false",
            "OKX_API_KEY": "key",
            "OKX_SECRET_KEY": "secret",
            "OKX_PASSPHRASE": "pass",
            "SYMBOL_STOP_LOSS_PCTS": "ETH:0.015",
            "SYMBOL_TAKE_PROFIT_PCTS": "ETH:0.06",
        })

        order = bot.create_swap_market_order_with_tp_sl(
            "ETH/USDT:USDT",
            Decimal("2"),
            Decimal("100"),
            order_side="buy",
            position_side="long",
        )

        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 106.0)
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 98.5)

    def test_position_mode_uses_detected_okx_net_mode(self) -> None:
        bot = make_bot(
            {"DRY_RUN": "false", "POSITION_MODE": "hedge", "OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass"},
            hedged=False,
        )

        params = bot.swap_order_params("short")

        self.assertEqual(params["positionSide"], "net")

    def test_position_mode_uses_detected_okx_hedge_mode(self) -> None:
        bot = make_bot(
            {"DRY_RUN": "false", "POSITION_MODE": "net", "OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass"},
            hedged=True,
        )

        params = bot.swap_order_params("short")

        self.assertEqual(params["positionSide"], "short")

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

    def test_same_direction_buy_adds_only_after_profit_and_breakout(self) -> None:
        bot = make_bot()
        symbol = "BTC/USDT:USDT"
        bot.state.record_trade("buy", Decimal("2"), Decimal("100"), Decimal("200"), "dry-run", symbol=symbol, position_side="long")
        signal = Signal("buy", "Add long.", Decimal("110"), {}, Decimal("1"))

        bot.buy(symbol, signal, long_breakout_candles())

        self.assertEqual(bot.state.get_position_side(symbol), "long")
        self.assertEqual(bot.state.get_add_count(symbol), 1)
        self.assertGreater(bot.state.get_position_base(symbol), Decimal("2"))

    def test_same_direction_buy_keeps_position_when_not_profitable(self) -> None:
        bot = make_bot()
        symbol = "BTC/USDT:USDT"
        bot.state.record_trade("buy", Decimal("2"), Decimal("100"), Decimal("200"), "dry-run", symbol=symbol, position_side="long")
        signal = Signal("buy", "Try add long.", Decimal("99"), {}, Decimal("1"))

        bot.buy(symbol, signal, long_breakout_candles())

        self.assertEqual(bot.state.get_add_count(symbol), 0)
        self.assertEqual(bot.state.get_position_base(symbol), Decimal("2"))

    def test_same_direction_buy_keeps_position_without_breakout_or_pullback(self) -> None:
        bot = make_bot()
        symbol = "BTC/USDT:USDT"
        bot.state.record_trade("buy", Decimal("2"), Decimal("100"), Decimal("200"), "dry-run", symbol=symbol, position_side="long")
        signal = Signal("buy", "Try add long.", Decimal("104"), {}, Decimal("1"))

        bot.buy(symbol, signal, long_no_add_candles())

        self.assertEqual(bot.state.get_add_count(symbol), 0)
        self.assertEqual(bot.state.get_position_base(symbol), Decimal("2"))


if __name__ == "__main__":
    unittest.main()
