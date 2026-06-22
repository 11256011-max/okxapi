from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .config import BotConfig
from .exchange import create_exchange
from .models import Candle, Signal
from .risk import RiskError, RiskManager
from .state import BotState
from .strategy import create_strategy


@dataclass(frozen=True)
class SwapPositionPlan:
    equity: Decimal
    risk_amount: Decimal
    margin_budget: Decimal
    notional: Decimal
    leverage: int
    amount_contracts: Decimal


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
        if self.config.attach_tp_sl and not self.config.dry_run:
            return signal
        if self.risk.stop_loss_hit(self.state, symbol, signal.price):
            return Signal(self.close_action_for_symbol(symbol), "Stop loss hit.", signal.price, signal.indicators, Decimal("1"))
        if self.risk.take_profit_hit(self.state, symbol, signal.price):
            return Signal(self.close_action_for_symbol(symbol), "Take profit hit.", signal.price, signal.indicators, Decimal("1"))
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
        self.buy_swap(symbol, signal)

    def sell(self, symbol: str, signal: Signal) -> None:
        self.sell_swap(symbol, signal)

    def buy_swap(self, symbol: str, signal: Signal) -> None:
        position_side = self.state.get_position_side(symbol)
        if position_side == "short":
            self.close_swap_position(symbol, signal, order_side="buy")
            return
        if position_side == "long":
            logging.info("Swap buy skipped because %s already has an open long position.", symbol)
            return
        self.open_swap_position(symbol, signal, position_side="long", order_side="buy")

    def sell_swap(self, symbol: str, signal: Signal) -> None:
        position_side = self.state.get_position_side(symbol)
        if position_side == "long":
            self.close_swap_position(symbol, signal, order_side="sell")
            return
        if position_side == "short":
            logging.info("Swap sell skipped because %s already has an open short position.", symbol)
            return
        self.open_swap_position(symbol, signal, position_side="short", order_side="sell")

    def open_swap_position(
        self,
        symbol: str,
        signal: Signal,
        position_side: str,
        order_side: str,
    ) -> None:
        try:
            plan = self.build_swap_position_plan(symbol, signal.price)
            self.assert_daily_loss_limit_not_hit(plan.equity)
        except RiskError as exc:
            logging.warning("Swap %s blocked by risk manager: %s", order_side, exc)
            return

        take_profit_price, stop_loss_price = self.exit_prices(signal.price, position_side)

        if self.config.dry_run:
            logging.info(
                "[DRY RUN] Would open %s %s: contracts=%s notional=%s margin_budget=%s leverage=%sx risk_amount=%s TP=%s SL=%s.",
                symbol,
                position_side,
                plan.amount_contracts,
                plan.notional,
                plan.margin_budget,
                plan.leverage,
                plan.risk_amount,
                take_profit_price,
                stop_loss_price,
            )
            self.state.record_trade(
                order_side,
                plan.amount_contracts,
                signal.price,
                plan.notional,
                mode="dry-run",
                symbol=symbol,
                position_side=position_side,
            )
            return

        self.assert_order_submission_allowed()
        try:
            self.set_swap_leverage(symbol, plan.leverage, position_side)
            order = self.create_swap_market_order_with_tp_sl(
                symbol,
                plan.amount_contracts,
                signal.price,
                order_side=order_side,
                position_side=position_side,
            )
        except Exception as exc:
            logging.warning("Swap %s failed due to order execution error: %s", order_side, exc)
            return

        average_price = self.decimal_from_order(order, "average", signal.price)
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        self.state.record_trade(
            order_side,
            plan.amount_contracts,
            average_price or signal.price,
            plan.notional,
            mode=self.execution_mode,
            order_id=order_id,
            symbol=symbol,
            position_side=position_side,
        )
        logging.info("Swap %s submitted with attached TP/SL: %s", position_side, order)

    def close_swap_position(self, symbol: str, signal: Signal, order_side: str) -> None:
        position_side = self.state.get_position_side(symbol)
        amount_contracts = self.state.get_position_base(symbol)
        amount_contracts = self.quantize_amount(amount_contracts * self.config.sell_fraction)
        if amount_contracts <= 0:
            logging.info("Swap close skipped because no local %s position is recorded.", symbol)
            return

        if self.config.dry_run:
            quote_notional = self.contract_notional(symbol, amount_contracts, signal.price)
            realized_pnl = self.calculate_swap_pnl(symbol, amount_contracts, signal.price)
            logging.info("[DRY RUN] Would close %s %s contracts=%s realized_pnl=%s.", symbol, position_side, amount_contracts, realized_pnl)
            self.state.record_trade(
                order_side,
                amount_contracts,
                signal.price,
                quote_notional,
                mode="dry-run",
                symbol=symbol,
                position_side=position_side,
                reduce_only=True,
                realized_pnl=realized_pnl,
            )
            return

        self.assert_order_submission_allowed()
        try:
            order = self.exchange.create_order(
                symbol,
                "market",
                order_side,
                float(amount_contracts),
                None,
                params=self.swap_order_params(position_side, reduce_only=True),
            )
        except Exception as exc:
            logging.warning("Swap close failed due to order execution error: %s", exc)
            return

        average_price = self.decimal_from_order(order, "average", signal.price)
        close_price = average_price or signal.price
        quote_notional = self.contract_notional(symbol, amount_contracts, close_price)
        realized_pnl = self.calculate_swap_pnl(symbol, amount_contracts, close_price)
        order_id = str(order.get("id")) if isinstance(order, dict) else None
        self.state.record_trade(
            order_side,
            amount_contracts,
            close_price,
            quote_notional,
            mode=self.execution_mode,
            order_id=order_id,
            symbol=symbol,
            position_side=position_side,
            reduce_only=True,
            realized_pnl=realized_pnl,
        )
        logging.info("Swap %s close submitted: %s", position_side, order)

    def build_swap_position_plan(self, symbol: str, entry_price: Decimal) -> SwapPositionPlan:
        if entry_price <= 0:
            raise RiskError("Entry price must be greater than 0.")

        equity = self.fetch_account_equity()
        if equity <= 0:
            raise RiskError("Account equity is unavailable or zero.")

        risk_amount = equity * self.config.risk_per_trade_pct
        max_notional_by_risk = risk_amount / self.config.stop_loss_pct
        margin_budget = min(self.config.order_quote_amount, self.config.max_quote_per_order, max_notional_by_risk)
        if margin_budget <= 0:
            raise RiskError("Margin budget is zero.")

        needed_leverage = self.ceil_decimal(max_notional_by_risk / margin_budget)
        leverage = max(1, min(self.config.max_leverage, needed_leverage))
        notional = min(max_notional_by_risk, margin_budget * Decimal(leverage))
        amount_contracts = self.contract_amount_from_notional(symbol, notional, entry_price)
        if amount_contracts <= 0:
            raise RiskError("Calculated contract amount is too small.")

        actual_notional = self.contract_notional(symbol, amount_contracts, entry_price)
        return SwapPositionPlan(
            equity=equity,
            risk_amount=risk_amount,
            margin_budget=margin_budget,
            notional=actual_notional,
            leverage=leverage,
            amount_contracts=amount_contracts,
        )

    def create_swap_market_order_with_tp_sl(
        self,
        symbol: str,
        amount_contracts: Decimal,
        entry_price: Decimal,
        order_side: str,
        position_side: str,
    ) -> dict[str, Any]:
        take_profit_price, stop_loss_price = self.exit_prices(entry_price, position_side)
        params = self.swap_order_params(position_side)
        if self.config.attach_tp_sl:
            params["takeProfit"] = {
                "triggerPrice": float(take_profit_price),
                "type": "market",
                "triggerPriceType": "last",
            }
            params["stopLoss"] = {
                "triggerPrice": float(stop_loss_price),
                "type": "market",
                "triggerPriceType": "last",
            }

        return self.exchange.create_order(
            symbol,
            "market",
            order_side,
            float(amount_contracts),
            None,
            params=params,
        )

    def assert_daily_loss_limit_not_hit(self, equity: Decimal) -> None:
        daily_loss_limit = equity * self.config.daily_max_loss_pct
        if self.state.daily_realized_pnl <= -daily_loss_limit:
            raise RiskError(
                f"Daily realized loss limit reached: {self.state.daily_realized_pnl} <= -{daily_loss_limit}"
            )

    def calculate_swap_pnl(self, symbol: str, amount_contracts: Decimal, exit_price: Decimal) -> Decimal:
        entry_price = self.state.get_entry_price(symbol)
        position_side = self.state.get_position_side(symbol)
        if entry_price <= 0 or amount_contracts <= 0:
            return Decimal("0")

        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        contract_size = Decimal(str(market.get("contractSize") or "1"))
        if position_side == "short":
            return (entry_price - exit_price) * amount_contracts * contract_size
        return (exit_price - entry_price) * amount_contracts * contract_size

    def set_swap_leverage(self, symbol: str, leverage: int, position_side: str) -> None:
        params: dict[str, Any] = {"mgnMode": self.config.margin_mode}
        if self.config.position_mode == "hedge":
            params["posSide"] = position_side
        self.exchange.set_leverage(
            leverage,
            symbol,
            params=params,
        )

    def swap_order_params(self, position_side: str | None = None, reduce_only: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"tdMode": self.config.margin_mode}
        if reduce_only:
            params["reduceOnly"] = True
        if self.config.position_mode == "hedge" and position_side in {"long", "short"}:
            params["positionSide"] = position_side
        return params

    def contract_amount_from_notional(self, symbol: str, notional: Decimal, price: Decimal) -> Decimal:
        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        contract_size = Decimal(str(market.get("contractSize") or "1"))
        raw_amount = notional / (price * contract_size)
        precise_amount = self.exchange.amount_to_precision(symbol, float(raw_amount))
        return Decimal(str(precise_amount))

    def contract_notional(self, symbol: str, amount_contracts: Decimal, price: Decimal) -> Decimal:
        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        contract_size = Decimal(str(market.get("contractSize") or "1"))
        return amount_contracts * contract_size * price

    def fetch_account_equity(self) -> Decimal:
        balance = self.exchange.fetch_balance(params={"type": self.config.market_type})
        info = balance.get("info", {})
        data = info.get("data") if isinstance(info, dict) else None
        if isinstance(data, list) and data:
            total_eq = data[0].get("totalEq")
            if total_eq not in (None, ""):
                return Decimal(str(total_eq))

        total = balance.get("total", {})
        free = balance.get("free", {})
        for source in (total, free):
            value = source.get("USDT") if isinstance(source, dict) else None
            if value not in (None, ""):
                return Decimal(str(value))
        return Decimal("0")

    def close_action_for_symbol(self, symbol: str) -> str:
        return "buy" if self.state.get_position_side(symbol) == "short" else "sell"

    def exit_prices(self, entry_price: Decimal, position_side: str = "long") -> tuple[Decimal, Decimal]:
        if position_side == "short":
            take_profit_price = entry_price * (Decimal("1") - self.config.take_profit_pct)
            stop_loss_price = entry_price * (Decimal("1") + self.config.stop_loss_pct)
            return take_profit_price, stop_loss_price
        take_profit_price = entry_price * (Decimal("1") + self.config.take_profit_pct)
        stop_loss_price = entry_price * (Decimal("1") - self.config.stop_loss_pct)
        return take_profit_price, stop_loss_price

    def print_balance(self) -> None:
        self.config.validate(require_private=True)
        balance = self.exchange.fetch_balance(params={"type": self.config.market_type})
        total = balance.get("total", {})
        free = balance.get("free", {})
        for currency, total_amount in sorted(total.items()):
            if total_amount:
                logging.info("%s total=%s free=%s", currency, total_amount, free.get(currency))

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
    def ceil_decimal(value: Decimal) -> int:
        rounded = int(value)
        return rounded if value == Decimal(rounded) else rounded + 1

    @staticmethod
    def format_confidence(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
