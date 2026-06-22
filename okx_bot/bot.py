from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .config import BotConfig
from .exchange import create_exchange
from .models import Candle, Signal
from .risk import RiskError, RiskManager
from .state import BotState
from .strategy import create_strategy


class TradingBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.exchange = create_exchange(config)
        self.strategy = create_strategy(config)
        self.risk = RiskManager(config)
        self.state = BotState.load(config.state_file)

    def run_once(self) -> None:
        self.state.reset_daily_if_needed()
        candles = self.fetch_candles()
        signal = self.strategy.generate(candles)
        signal = self.apply_signal_confidence_gate(signal)
        signal = self.apply_position_risk(signal)

        logging.info(
            "Signal=%s confidence=%s price=%s reason=%s indicators=%s",
            signal.action,
            self.format_confidence(signal.confidence),
            signal.price,
            signal.reason,
            signal.indicators,
        )

        self.execute_signal(signal)
        self.state.save(self.config.state_file)

    def fetch_candles(self) -> list[Candle]:
        raw_candles = self.exchange.fetch_ohlcv(
            self.config.symbol,
            timeframe=self.config.timeframe,
            limit=self.config.candle_limit,
        )
        candles = [
            Candle(
                timestamp=int(row[0]),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
            )
            for row in raw_candles
        ]
        if not candles:
            raise RuntimeError("No candles returned by exchange.")
        return candles

    def apply_position_risk(self, signal: Signal) -> Signal:
        if self.risk.stop_loss_hit(self.state, signal.price):
            return Signal("sell", "Stop loss hit.", signal.price, signal.indicators, Decimal("1"))
        if self.risk.take_profit_hit(self.state, signal.price):
            return Signal("sell", "Take profit hit.", signal.price, signal.indicators, Decimal("1"))
        return signal

    def apply_signal_confidence_gate(self, signal: Signal) -> Signal:
        if signal.action not in {"buy", "sell"}:
            return signal
        if signal.confidence >= self.config.signal_confidence_threshold:
            return signal

        indicators = {**signal.indicators, "confidence": float(signal.confidence)}
        reason = (
            f"{signal.action.upper()} signal blocked because confidence "
            f"{self.format_confidence(signal.confidence)} is below threshold "
            f"{self.format_confidence(self.config.signal_confidence_threshold)}. {signal.reason}"
        )
        return Signal("hold", reason, signal.price, indicators, signal.confidence)

    def execute_signal(self, signal: Signal) -> None:
        if signal.action == "hold":
            return
        if signal.action == "buy":
            self.buy(signal)
            return
        if signal.action == "sell":
            self.sell(signal)
            return
        raise RuntimeError(f"Unknown signal action: {signal.action}")

    def buy(self, signal: Signal) -> None:
        if self.state.position_base > 0:
            logging.info("Buy skipped because state already has an open spot position.")
            return

        try:
            decision = self.risk.approve_buy(self.state)
        except RiskError as exc:
            logging.warning("Buy blocked by risk manager: %s", exc)
            return

        quote_amount = decision.quote_amount
        amount_base = self.quantize_amount(quote_amount / signal.price)

        if self.config.dry_run:
            logging.info("[DRY RUN] Would buy about %s %s for %s quote.", amount_base, self.config.symbol, quote_amount)
            self.state.record_trade("buy", amount_base, signal.price, quote_amount, mode="dry-run")
            return

        self.assert_order_submission_allowed()
        order = self.create_market_buy(quote_amount)
        filled_base = self.decimal_from_order(order, "filled", amount_base)
        average_price = self.decimal_from_order(order, "average", signal.price)
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        self.state.record_trade("buy", filled_base, average_price, quote_amount, mode=self.execution_mode, order_id=order_id)
        logging.info("Buy submitted: %s", order)

    def sell(self, signal: Signal) -> None:
        amount_base = self.state.position_base
        if amount_base <= 0 and not self.config.dry_run:
            amount_base = self.fetch_base_free_balance()
        amount_base = self.quantize_amount(amount_base * self.config.sell_fraction)

        if amount_base <= 0:
            logging.info("Sell skipped because no base balance is available.")
            return

        quote_notional = amount_base * signal.price

        if self.config.dry_run:
            logging.info("[DRY RUN] Would sell %s %s.", amount_base, self.config.symbol)
            self.state.record_trade("sell", amount_base, signal.price, quote_notional, mode="dry-run")
            return

        self.assert_order_submission_allowed()
        order = self.exchange.create_market_sell_order(
            self.config.symbol,
            float(amount_base),
            params={"tdMode": "cash"},
        )
        average_price = self.decimal_from_order(order, "average", signal.price)
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        self.state.record_trade("sell", amount_base, average_price, quote_notional, mode=self.execution_mode, order_id=order_id)
        logging.info("Sell submitted: %s", order)

    def create_market_buy(self, quote_amount: Decimal) -> dict[str, Any]:
        if hasattr(self.exchange, "create_market_buy_order_with_cost"):
            return self.exchange.create_market_buy_order_with_cost(
                self.config.symbol,
                float(quote_amount),
                params={"tdMode": "cash"},
            )
        ticker = self.exchange.fetch_ticker(self.config.symbol)
        last_price = Decimal(str(ticker["last"]))
        amount_base = self.quantize_amount(quote_amount / last_price)
        return self.exchange.create_market_buy_order(
            self.config.symbol,
            float(amount_base),
            params={"tdMode": "cash"},
        )

    def print_balance(self) -> None:
        self.config.validate(require_private=True)
        balance = self.exchange.fetch_balance()
        total = balance.get("total", {})
        free = balance.get("free", {})
        for currency, total_amount in sorted(total.items()):
            if total_amount:
                logging.info("%s total=%s free=%s", currency, total_amount, free.get(currency))

    def fetch_base_free_balance(self) -> Decimal:
        base_currency = self.config.symbol.split("/")[0]
        balance = self.exchange.fetch_balance()
        free = balance.get("free", {})
        return Decimal(str(free.get(base_currency, "0") or "0"))

    def assert_order_submission_allowed(self) -> None:
        if self.config.dry_run:
            return
        if not self.config.okx_simulated_trading and not self.config.enable_live_trading:
            raise RuntimeError("Live order blocked by ENABLE_LIVE_TRADING=false.")

    @property
    def execution_mode(self) -> str:
        if self.config.dry_run:
            return "dry-run"
        if self.config.okx_simulated_trading:
            return "okx-simulated"
        return "live"

    @staticmethod
    def decimal_from_order(order: Any, key: str, fallback: Decimal) -> Decimal:
        if isinstance(order, dict) and order.get(key) not in (None, ""):
            return Decimal(str(order[key]))
        return fallback

    @staticmethod
    def quantize_amount(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    @staticmethod
    def format_confidence(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
