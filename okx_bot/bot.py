from __future__ import annotations

import logging
import time
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
        self.state = BotState.load(config.state_file, default_symbol=self.config.symbols[0])

    def run_once(self) -> None:
        self.state.reset_daily_if_needed()
        for symbol in self.config.symbols:
            self.ensure_protective_order_for_position(symbol)
            try:
                candles = self.fetch_candles(symbol)
            except Exception as exc:
                logging.warning("Skipping %s because candle fetch failed: %s", symbol, exc)
                continue

            signal = self.strategy.generate(candles)
            signal = self.apply_signal_confidence_gate(signal)
            signal = self.apply_position_risk(symbol, signal)

            logging.info(
                "Symbol=%s Signal=%s confidence=%s price=%s reason=%s indicators=%s",
                symbol,
                signal.action,
                self.format_confidence(signal.confidence),
                signal.price,
                signal.reason,
                signal.indicators,
            )

            self.execute_signal(symbol, signal)

        self.state.save(self.config.state_file)

    def fetch_candles(self, symbol: str) -> list[Candle]:
        raw_candles = self.exchange.fetch_ohlcv(
            symbol,
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

    def apply_position_risk(self, symbol: str, signal: Signal) -> Signal:
        if self.config.attach_tp_sl and self.state.get_protective_algo_id(symbol) and not self.config.dry_run:
            return signal
        if self.risk.stop_loss_hit(self.state, symbol, signal.price):
            return Signal("sell", "Stop loss hit.", signal.price, signal.indicators, Decimal("1"))
        if self.risk.take_profit_hit(self.state, symbol, signal.price):
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

    def execute_signal(self, symbol: str, signal: Signal) -> None:
        if signal.action == "hold":
            return
        if signal.action == "buy":
            self.buy(symbol, signal)
            return
        if signal.action == "sell":
            self.sell(symbol, signal)
            return
        raise RuntimeError(f"Unknown signal action: {signal.action}")

    def buy(self, symbol: str, signal: Signal) -> None:
        current_position = self.state.get_position_base(symbol)
        if current_position > 0:
            logging.info("Adding to existing position for %s: current base=%s", symbol, current_position)

        try:
            decision = self.risk.approve_buy(self.state)
        except RiskError as exc:
            logging.warning("Buy blocked by risk manager: %s", exc)
            return

        quote_amount = decision.quote_amount
        amount_base = self.quantize_amount(quote_amount / signal.price)

        if self.config.dry_run:
            logging.info("[DRY RUN] Would buy about %s %s for %s quote.", amount_base, symbol, quote_amount)
            self.state.record_trade("buy", amount_base, signal.price, quote_amount, mode="dry-run", symbol=symbol)
            return

        self.assert_order_submission_allowed()
        free_quote = self.fetch_quote_free_balance(symbol)
        if free_quote <= 0:
            logging.warning("Buy skipped because no available quote balance is available.")
            return
        if quote_amount > free_quote:
            logging.warning(
                "Requested buy quote amount %s exceeds available quote balance %s; reducing order size.",
                quote_amount,
                free_quote,
            )
            quote_amount = free_quote
            amount_base = self.quantize_amount(quote_amount / signal.price)
            if amount_base <= 0:
                logging.warning("Buy skipped because reduced order size is too small.")
                return

        base_balance_before = self.fetch_base_free_balance(symbol)

        try:
            order = self.create_market_buy(quote_amount, symbol)
        except Exception as exc:
            logging.warning("Buy failed due to order execution error: %s", exc)
            return
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        filled_base, average_price = self.resolve_buy_fill(
            order=order,
            symbol=symbol,
            quote_amount=quote_amount,
            base_balance_before=base_balance_before,
            fallback_price=signal.price,
        )

        if filled_base <= 0 or average_price <= 0:
            logging.warning(
                "Buy order was submitted but no filled base balance could be confirmed yet; "
                "skipping local position record and protective TP/SL for now. order_id=%s",
                order_id,
            )
            return

        self.state.record_trade(
            "buy",
            filled_base,
            average_price,
            quote_amount,
            mode=self.execution_mode,
            order_id=order_id,
            symbol=symbol,
        )
        logging.info("Buy submitted: %s", order)

        try:
            self.place_protective_order_after_buy(filled_base, average_price, symbol)
        except Exception:
            logging.exception("Failed to place protective OKX OCO TP/SL. Internal TP/SL monitor remains active.")

    def sell(self, symbol: str, signal: Signal) -> None:
        amount_base = self.state.get_position_base(symbol)
        if not self.config.dry_run:
            free_base = self.fetch_base_free_balance(symbol)
            if free_base <= 0:
                logging.info(
                    "Sell skipped for %s because no free base balance is available. Clearing local position state.",
                    symbol,
                )
                self.state.clear_symbol_position(symbol)
                return
            if amount_base <= 0:
                amount_base = free_base
            else:
                amount_base = min(amount_base, free_base)
        amount_base = self.quantize_amount(amount_base * self.config.sell_fraction)

        if amount_base <= 0:
            logging.info("Sell skipped because no base balance is available.")
            return

        quote_notional = amount_base * signal.price

        if self.config.dry_run:
            logging.info("[DRY RUN] Would sell %s %s.", amount_base, symbol)
            self.state.record_trade("sell", amount_base, signal.price, quote_notional, mode="dry-run", symbol=symbol)
            return

        self.assert_order_submission_allowed()
        self.cancel_protective_order_if_present(symbol)
        order = self.exchange.create_market_sell_order(
            symbol,
            float(amount_base),
            params={"tdMode": "cash"},
        )
        average_price = self.decimal_from_order(order, "average", signal.price)
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        self.state.record_trade(
            "sell",
            amount_base,
            average_price,
            quote_notional,
            mode=self.execution_mode,
            order_id=order_id,
            symbol=symbol,
        )
        logging.info("Sell submitted: %s", order)

    def place_protective_order_after_buy(self, amount_base: Decimal, entry_price: Decimal, symbol: str) -> None:
        if not self.config.attach_tp_sl:
            return
        if amount_base <= 0 or entry_price <= 0:
            logging.warning("Protective TP/SL skipped because filled amount or entry price is invalid.")
            return
        if self.config.dry_run:
            take_profit_price, stop_loss_price = self.exit_prices(entry_price)
            logging.info(
                "[DRY RUN] Would place OKX OCO TP/SL for %s: amount=%s take_profit=%s stop_loss=%s.",
                symbol,
                amount_base,
                take_profit_price,
                stop_loss_price,
            )
            return

        available_base = self.wait_for_available_base(symbol, amount_base)
        if available_base <= 0:
            logging.warning("Protective TP/SL skipped because no available %s balance was confirmed.", symbol)
            return

        protect_amount = self.quantize_amount(min(amount_base, available_base))
        if protect_amount <= 0:
            logging.warning("Protective TP/SL skipped because available %s amount is too small.", symbol)
            return

        payload = self.build_protective_oco_payload(protect_amount, entry_price, symbol)
        response = self.exchange.private_post_trade_order_algo(payload)
        algo_id, algo_cl_ord_id = self.extract_algo_identifiers(response, payload.get("algoClOrdId"))
        self.state.set_protective_order(symbol, algo_id, algo_cl_ord_id)
        logging.info("Protective OKX OCO TP/SL submitted: %s", response)

    def ensure_protective_order_for_position(self, symbol: str) -> None:
        if self.config.dry_run or not self.config.attach_tp_sl:
            return
        if self.state.get_protective_algo_id(symbol):
            return

        position_base = self.state.get_position_base(symbol)
        entry_price = self.state.get_entry_price(symbol)
        if position_base <= 0 or entry_price <= 0:
            return

        try:
            self.place_protective_order_after_buy(position_base, entry_price, symbol)
            logging.info("Protective OKX OCO TP/SL restored for existing %s position.", symbol)
        except Exception:
            logging.exception("Failed to restore protective OKX OCO TP/SL for existing %s position.", symbol)

    def resolve_buy_fill(
        self,
        order: dict[str, Any],
        symbol: str,
        quote_amount: Decimal,
        base_balance_before: Decimal,
        fallback_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        order_id = str(order.get("id")) if isinstance(order, dict) and order.get("id") else None
        for attempt in range(8):
            fetched_order = self.fetch_order_safely(order_id, symbol)
            filled_base = self.decimal_from_order(fetched_order, "filled", Decimal("0"))
            average_price = self.decimal_from_order(fetched_order, "average", Decimal("0"))
            if filled_base > 0 and average_price > 0:
                return self.quantize_amount(filled_base), average_price

            base_balance_after = self.fetch_base_free_balance(symbol)
            balance_delta = base_balance_after - base_balance_before
            if balance_delta > 0:
                average_from_cost = quote_amount / balance_delta
                return self.quantize_amount(balance_delta), average_price or average_from_cost or fallback_price

            if attempt < 7:
                time.sleep(0.5)

        return Decimal("0"), Decimal("0")

    def fetch_order_safely(self, order_id: str | None, symbol: str) -> dict[str, Any]:
        if not order_id:
            return {}
        try:
            order = self.exchange.fetch_order(order_id, symbol)
            return order if isinstance(order, dict) else {}
        except Exception:
            return {}

    def wait_for_available_base(self, symbol: str, desired_amount: Decimal) -> Decimal:
        for attempt in range(8):
            available_base = self.fetch_base_free_balance(symbol)
            if available_base >= desired_amount or (available_base > 0 and attempt >= 2):
                return available_base
            if attempt < 7:
                time.sleep(0.5)
        return self.fetch_base_free_balance(symbol)

    def build_protective_oco_payload(self, amount_base: Decimal, entry_price: Decimal, symbol: str) -> dict[str, str]:
        take_profit_price, stop_loss_price = self.exit_prices(entry_price)
        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        inst_id = market["id"]
        amount = self.exchange.amount_to_precision(symbol, float(amount_base))
        take_profit = self.exchange.price_to_precision(symbol, float(take_profit_price))
        stop_loss = self.exchange.price_to_precision(symbol, float(stop_loss_price))

        return {
            "instId": inst_id,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "oco",
            "sz": amount,
            "tpTriggerPx": take_profit,
            "tpOrdPx": "-1",
            "tpTriggerPxType": "last",
            "slTriggerPx": stop_loss,
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
            "algoClOrdId": self.new_algo_client_id(),
        }

    def cancel_protective_order_if_present(self, symbol: str) -> None:
        algo_id = self.state.get_protective_algo_id(symbol)
        if not algo_id:
            return

        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        payload = [{"algoId": algo_id, "instId": market["id"]}]
        try:
            response = self.exchange.private_post_trade_cancel_algos(payload)
            logging.info("Protective OKX OCO TP/SL canceled before market sell: %s", response)
            self.state.clear_protective_order(symbol)
        except Exception:
            logging.exception("Failed to cancel protective OKX OCO TP/SL before market sell.")

    def exit_prices(self, entry_price: Decimal) -> tuple[Decimal, Decimal]:
        take_profit_price = entry_price * (Decimal("1") + self.config.take_profit_pct)
        stop_loss_price = entry_price * (Decimal("1") - self.config.stop_loss_pct)
        return take_profit_price, stop_loss_price

    def create_market_buy(self, quote_amount: Decimal, symbol: str) -> dict[str, Any]:
        if hasattr(self.exchange, "create_market_buy_order_with_cost"):
            return self.exchange.create_market_buy_order_with_cost(
                symbol,
                float(quote_amount),
                params={"tdMode": "cash"},
            )
        ticker = self.exchange.fetch_ticker(symbol)
        last_price = Decimal(str(ticker["last"]))
        amount_base = self.quantize_amount(quote_amount / last_price)
        return self.exchange.create_market_buy_order(
            symbol,
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

    def fetch_quote_free_balance(self, symbol: str) -> Decimal:
        quote_currency = symbol.split("/")[1]
        balance = self.exchange.fetch_balance()
        free = balance.get("free", {})
        return Decimal(str(free.get(quote_currency, "0") or "0"))

    def fetch_base_free_balance(self, symbol: str) -> Decimal:
        base_currency = symbol.split("/")[0]
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
    def extract_algo_identifiers(response: Any, fallback_algo_cl_ord_id: str | None) -> tuple[str | None, str | None]:
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    return first.get("algoId") or None, first.get("algoClOrdId") or fallback_algo_cl_ord_id
        return None, fallback_algo_cl_ord_id

    @staticmethod
    def new_algo_client_id() -> str:
        return f"codex{int(time.time() * 1000)}"[:32]

    @staticmethod
    def format_confidence(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
