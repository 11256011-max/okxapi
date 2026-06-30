from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .config import BotConfig
from .exchange import create_exchange
from .exit_plan import build_exit_plan
from .external_context import ExternalContextService
from .models import Candle, Signal
from .state import BotState
from .strategy import create_strategy


class RiskError(RuntimeError):
    """Raised when a trade violates a configured risk rule."""


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
        self.external_context = ExternalContextService(config) if config.external_context_enabled else None
        self.state = BotState.load(config.state_file, default_symbol=self.config.symbols[0])
        self._effective_position_mode: str | None = None

    def run_once(self) -> None:
        self.state.reset_daily_if_needed()
        for symbol in self.config.symbols:
            try:
                candles_by_timeframe = self.fetch_analysis_candles(symbol)
            except Exception as exc:
                logging.warning("Skipping %s because candle fetch failed: %s", symbol, exc)
                continue

            entry_candles = candles_by_timeframe[self.config.entry_timeframe]
            if self.manage_open_position(symbol, entry_candles):
                continue

            signal = self.strategy.generate_multi(candles_by_timeframe)
            signal = self.apply_external_context_filter(symbol, signal)
            signal = self.apply_signal_confidence_gate(symbol, signal)

            logging.info(
                "Symbol=%s Signal=%s confidence=%s price=%s reason=%s indicators=%s",
                symbol,
                signal.action,
                self.format_confidence(signal.confidence),
                signal.price,
                signal.reason,
                signal.indicators,
            )

            self.execute_signal(symbol, signal, entry_candles)

        self.state.save(self.config.state_file)

    def fetch_analysis_candles(self, symbol: str) -> dict[str, list[Candle]]:
        return {
            timeframe: self.fetch_candles(symbol, timeframe)
            for timeframe in self.config.analysis_timeframes
        }

    def fetch_candles(self, symbol: str, timeframe: str | None = None) -> list[Candle]:
        timeframe = timeframe or self.config.entry_timeframe
        raw_candles = self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
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

    def manage_open_position(self, symbol: str, candles: list[Candle]) -> bool:
        if not candles:
            return False
        position = self.state.ensure_symbol(symbol)
        if position.position_base <= 0 or position.side not in {"long", "short"}:
            return False

        latest = candles[-1]
        self.ensure_position_exit_state(symbol, latest.close)
        if position.risk_per_unit <= 0 or position.stop_loss_price <= 0:
            logging.warning("Dynamic exit skipped for %s because stop/risk state is unavailable.", symbol)
            return False

        self.update_position_extremes(position, latest)
        if self.position_stop_hit(position, latest):
            close_price = position.stop_loss_price
            reason = "dynamic stop loss"
            if position.trailing_armed or position.partial_taken:
                reason = "dynamic trailing stop"
            elif position.breakeven_armed:
                reason = "dynamic breakeven stop"
            return self.close_swap_position_fraction(symbol, close_price, Decimal("1"), reason)

        if self.position_reached_r(position, latest, self.config.exit_breakeven_r):
            self.arm_breakeven_stop(symbol, position)

        position_changed = False
        if (
            self.config.exit_partial_enabled
            and not position.partial_taken
            and self.position_reached_r(position, latest, self.config.exit_partial_take_profit_r)
        ):
            partial_price = self.price_at_r(position.side, position.entry_price, position.risk_per_unit, self.config.exit_partial_take_profit_r)
            if self.close_swap_position_fraction(symbol, partial_price, self.config.exit_partial_fraction, "dynamic partial take profit"):
                position.partial_taken = True
                position.trailing_armed = True
                self.arm_breakeven_stop(symbol, position)
                logging.info(
                    "Dynamic partial take profit completed for %s %s at %s; remaining contracts=%s.",
                    symbol,
                    position.side,
                    partial_price,
                    position.position_base,
                )
                position_changed = True

        if self.position_reached_r(position, latest, self.config.exit_trailing_start_r):
            self.arm_trailing_stop(symbol, position)

        self.update_trailing_stop(symbol, position, candles)
        if self.position_stop_hit(position, latest):
            return self.close_swap_position_fraction(symbol, position.stop_loss_price, Decimal("1"), "dynamic trailing stop")
        return position_changed

    def ensure_position_exit_state(self, symbol: str, current_price: Decimal) -> None:
        position = self.state.ensure_symbol(symbol)
        if position.entry_price <= 0 or position.side not in {"long", "short"}:
            return
        if position.highest_price <= 0:
            position.highest_price = max(position.entry_price, current_price)
        if position.lowest_price <= 0:
            position.lowest_price = min(position.entry_price, current_price)
        if position.stop_loss_price <= 0:
            _, stop_loss_price = self.exit_prices(position.entry_price, position.side, symbol)
            position.stop_loss_price = stop_loss_price
            position.initial_stop_loss_price = stop_loss_price
        if position.risk_per_unit <= 0:
            position.risk_per_unit = abs(position.entry_price - position.stop_loss_price)

    @staticmethod
    def update_position_extremes(position: Any, candle: Candle) -> None:
        position.highest_price = max(position.highest_price, candle.high)
        position.lowest_price = min(position.lowest_price, candle.low) if position.lowest_price > 0 else candle.low

    @staticmethod
    def position_stop_hit(position: Any, candle: Candle) -> bool:
        if position.stop_loss_price <= 0:
            return False
        if position.side == "short":
            return candle.high >= position.stop_loss_price
        return candle.low <= position.stop_loss_price

    def position_reached_r(self, position: Any, candle: Candle, reward_risk: Decimal) -> bool:
        target = self.price_at_r(position.side, position.entry_price, position.risk_per_unit, reward_risk)
        if position.side == "short":
            return candle.low <= target
        return candle.high >= target

    def arm_breakeven_stop(self, symbol: str, position: Any) -> None:
        if position.side == "short":
            new_stop = min(position.stop_loss_price, position.entry_price)
        else:
            new_stop = max(position.stop_loss_price, position.entry_price)
        if new_stop != position.stop_loss_price or not position.breakeven_armed:
            logging.info("Dynamic exit moved %s %s stop to breakeven at %s.", symbol, position.side, new_stop)
        position.stop_loss_price = new_stop
        position.breakeven_armed = True

    @staticmethod
    def arm_trailing_stop(symbol: str, position: Any) -> None:
        if not position.trailing_armed:
            logging.info("Dynamic exit armed %s %s trailing stop.", symbol, position.side)
        position.trailing_armed = True

    def update_trailing_stop(self, symbol: str, position: Any, candles: list[Candle]) -> None:
        if not self.config.exit_trailing_enabled or not (position.trailing_armed or position.partial_taken):
            return
        atr = self.average_true_range(candles[-(self.config.dynamic_exit_atr_period + 1) :])
        if atr <= 0:
            return
        trailing_distance = atr * self.config.exit_trailing_atr_multiplier
        if position.side == "short":
            candidate = position.lowest_price + trailing_distance
            if candidate < position.stop_loss_price:
                logging.info("Dynamic trailing stop moved %s short stop from %s to %s.", symbol, position.stop_loss_price, candidate)
                position.stop_loss_price = candidate
            return
        candidate = position.highest_price - trailing_distance
        if candidate > position.stop_loss_price:
            logging.info("Dynamic trailing stop moved %s long stop from %s to %s.", symbol, position.stop_loss_price, candidate)
            position.stop_loss_price = candidate

    def apply_external_context_filter(self, symbol: str, signal: Signal) -> Signal:
        if self.external_context is None:
            return signal

        snapshot = self.external_context.evaluate(symbol)
        indicators = {
            **signal.indicators,
            "strategy_confidence": float(signal.confidence),
            "external_context_score": float(snapshot.combined_score),
            "external_context_sources": float(snapshot.sources_used),
            "risk_multiplier": float(signal.indicators.get("risk_multiplier", 1.0)),
        }
        self.add_optional_indicator(indicators, "newsapi_score", snapshot.newsapi_score)
        self.add_optional_indicator(indicators, "gdelt_score", snapshot.gdelt_score)
        self.add_optional_indicator(indicators, "fear_greed_score", snapshot.fear_greed_score)
        self.add_optional_indicator(indicators, "fundamental_score", snapshot.fundamental_score)

        if signal.action not in {"buy", "sell"}:
            reason = (
                f"{signal.reason} External context score="
                f"{snapshot.combined_score.quantize(Decimal('0.01'))}, sources={snapshot.sources_used}."
            )
            return Signal(signal.action, reason, signal.price, indicators, signal.confidence)

        if snapshot.sources_used <= 0:
            reason = f"{signal.reason} External context unavailable; keeping strategy signal unchanged."
            return Signal(signal.action, reason, signal.price, indicators, signal.confidence)

        direction = Decimal("1") if signal.action == "buy" else Decimal("-1")
        directional_support = snapshot.combined_score * direction
        indicators["external_context_support"] = float(directional_support)
        risk_multiplier = self.external_context_risk_multiplier(snapshot.combined_score, snapshot.fear_greed_score, directional_support)
        indicators["risk_multiplier"] = float(risk_multiplier)
        adjusted_confidence = self.clamp_decimal(
            signal.confidence + (directional_support * self.config.external_context_max_confidence_adjustment)
        )

        if directional_support < self.config.external_context_min_support:
            indicators["confidence"] = float(adjusted_confidence)
            reason = (
                f"{signal.action.upper()} blocked by external context support "
                f"{directional_support.quantize(Decimal('0.01'))}. {signal.reason}"
            )
            return Signal("hold", reason, signal.price, indicators, adjusted_confidence)

        indicators["confidence"] = float(adjusted_confidence)
        risk_note = ""
        if risk_multiplier < Decimal("1"):
            risk_note = f" Risk multiplier reduced to {risk_multiplier} because external context is extreme or not supportive."
        reason = (
            f"{signal.reason} External context support="
            f"{directional_support.quantize(Decimal('0.01'))}, confidence adjusted to "
            f"{self.format_confidence(adjusted_confidence)}.{risk_note}"
        )
        return Signal(signal.action, reason, signal.price, indicators, adjusted_confidence)

    def external_context_risk_multiplier(
        self,
        combined_score: Decimal,
        fear_greed_score: Decimal | None,
        directional_support: Decimal,
    ) -> Decimal:
        risk_multiplier = Decimal("1")
        extreme_scores = [combined_score]
        if fear_greed_score is not None:
            extreme_scores.append(fear_greed_score)
        if any(abs(score) >= self.config.external_context_extreme_threshold for score in extreme_scores):
            risk_multiplier = min(risk_multiplier, self.config.external_context_risk_multiplier)
        if directional_support < Decimal("0"):
            risk_multiplier = min(risk_multiplier, self.config.external_context_risk_multiplier)
        return risk_multiplier

    def apply_signal_confidence_gate(self, symbol: str, signal: Signal) -> Signal:
        if signal.action not in {"buy", "sell"}:
            return signal
        threshold = self.config.confidence_threshold_for_symbol_and_action(symbol, signal.action)
        if signal.confidence >= threshold:
            return signal

        indicators = {**signal.indicators, "confidence": float(signal.confidence)}
        reason = (
            f"{signal.action.upper()} signal blocked because confidence "
            f"{self.format_confidence(signal.confidence)} is below threshold "
            f"{self.format_confidence(threshold)} for {symbol}. {signal.reason}"
        )
        return Signal("hold", reason, signal.price, indicators, signal.confidence)

    def execute_signal(self, symbol: str, signal: Signal, candles: list[Candle] | None = None) -> None:
        if signal.action == "hold":
            return
        if signal.action == "buy":
            self.buy(symbol, signal, candles)
            return
        if signal.action == "sell":
            self.sell(symbol, signal, candles)
            return
        raise RuntimeError(f"Unknown signal action: {signal.action}")

    def buy(self, symbol: str, signal: Signal, candles: list[Candle] | None = None) -> None:
        self.buy_swap(symbol, signal, candles)

    def sell(self, symbol: str, signal: Signal, candles: list[Candle] | None = None) -> None:
        self.sell_swap(symbol, signal, candles)

    def buy_swap(self, symbol: str, signal: Signal, candles: list[Candle] | None = None) -> None:
        position_side = self.state.get_position_side(symbol)
        if position_side == "short":
            if not self.config.exit_close_on_opposite_signal:
                logging.info("Buy signal ignored for %s because EXIT_CLOSE_ON_OPPOSITE_SIGNAL=false and a short is open.", symbol)
                return
            self.close_swap_position(symbol, signal, order_side="buy")
            return
        if position_side == "long":
            self.add_to_swap_position_if_allowed(symbol, signal, "long", "buy", candles)
            return
        self.open_swap_position(symbol, signal, position_side="long", order_side="buy", candles=candles)

    def sell_swap(self, symbol: str, signal: Signal, candles: list[Candle] | None = None) -> None:
        position_side = self.state.get_position_side(symbol)
        if position_side == "long":
            if not self.config.exit_close_on_opposite_signal:
                logging.info("Sell signal ignored for %s because EXIT_CLOSE_ON_OPPOSITE_SIGNAL=false and a long is open.", symbol)
                return
            self.close_swap_position(symbol, signal, order_side="sell")
            return
        if position_side == "short":
            self.add_to_swap_position_if_allowed(symbol, signal, "short", "sell", candles)
            return
        self.open_swap_position(symbol, signal, position_side="short", order_side="sell", candles=candles)

    def add_to_swap_position_if_allowed(
        self,
        symbol: str,
        signal: Signal,
        position_side: str,
        order_side: str,
        candles: list[Candle] | None,
    ) -> None:
        allowed, reason, indicators = self.evaluate_add_position(symbol, position_side, signal, candles)
        if not allowed:
            logging.info("Swap add skipped for %s %s: %s Keeping existing position.", symbol, position_side, reason)
            return

        add_signal = Signal(
            signal.action,
            f"{signal.reason} Add position approved: {reason}",
            signal.price,
            {**signal.indicators, **indicators},
            signal.confidence,
        )
        self.open_swap_position(
            symbol,
            add_signal,
            position_side=position_side,
            order_side=order_side,
            size_multiplier=self.config.add_position_quote_fraction,
            action_label="add",
            candles=candles,
        )

    def evaluate_add_position(
        self,
        symbol: str,
        position_side: str,
        signal: Signal,
        candles: list[Candle] | None,
    ) -> tuple[bool, str, dict[str, float]]:
        indicators: dict[str, float] = {
            "add_count": float(self.state.get_add_count(symbol)),
        }
        if not self.config.add_position_enabled:
            return False, "ADD_POSITION_ENABLED=false.", indicators
        if self.state.get_add_count(symbol) >= self.config.max_position_adds:
            return False, f"max add count reached ({self.config.max_position_adds}).", indicators
        if candles is None:
            return False, "no candle context for add rules.", indicators

        minimum_candles = max(
            self.config.add_position_breakout_lookback,
            self.config.add_position_pullback_ma_period + 1,
            self.config.add_position_support_lookback,
        ) + 1
        if len(candles) < minimum_candles:
            return False, f"not enough candles for add rules ({len(candles)} < {minimum_candles}).", indicators

        profit_pct = self.position_profit_pct(symbol, signal.price)
        indicators["position_profit_pct"] = float(profit_pct)
        if self.config.add_position_require_profit and profit_pct < self.config.add_position_min_profit_pct:
            return (
                False,
                f"position profit {self.format_percent(profit_pct)} below add threshold {self.format_percent(self.config.add_position_min_profit_pct)}.",
                indicators,
            )

        trend_clear, trend_indicators = self.add_position_trend_clear(candles, position_side)
        breakout, breakout_indicators = self.add_position_breakout(candles, position_side)
        pullback, pullback_indicators = self.add_position_pullback(candles, position_side)
        indicators.update(trend_indicators)
        indicators.update(breakout_indicators)
        indicators.update(pullback_indicators)

        if not trend_clear:
            return False, "trend is not clear enough for pyramiding.", indicators
        if breakout:
            return True, "trend breakout with volume confirmed.", indicators
        if pullback:
            return True, "pullback support or moving-average bounce confirmed.", indicators
        return False, "no breakout or pullback support confirmation.", indicators

    def open_swap_position(
        self,
        symbol: str,
        signal: Signal,
        position_side: str,
        order_side: str,
        size_multiplier: Decimal = Decimal("1"),
        action_label: str = "open",
        candles: list[Candle] | None = None,
    ) -> None:
        exit_plan = build_exit_plan(self.config, symbol, signal.price, position_side, signal, candles)
        risk_multiplier = self.signal_risk_multiplier(signal)
        try:
            self.assert_loss_streak_limit_not_hit()
            plan = self.build_swap_position_plan(
                symbol,
                signal.price,
                size_multiplier=size_multiplier * risk_multiplier,
                stop_loss_pct=exit_plan.stop_loss_pct,
            )
            self.assert_daily_loss_limit_not_hit(plan.equity)
        except RiskError as exc:
            logging.warning("Swap %s blocked by risk controls: %s", order_side, exc)
            return

        if self.config.dry_run:
            logging.info(
                "[DRY RUN] Would %s %s %s: contracts=%s notional=%s margin_budget=%s leverage=%sx risk_amount=%s TP=%s SL=%s exit_rr=%s dynamic_exit=%s.",
                action_label,
                symbol,
                position_side,
                plan.amount_contracts,
                plan.notional,
                plan.margin_budget,
                plan.leverage,
                plan.risk_amount,
                exit_plan.take_profit_price,
                exit_plan.stop_loss_price,
                exit_plan.reward_risk,
                exit_plan.dynamic,
            )
            self.state.record_trade(
                order_side,
                plan.amount_contracts,
                signal.price,
                plan.notional,
                mode="dry-run",
                symbol=symbol,
                position_side=position_side,
                stop_loss_price=exit_plan.stop_loss_price,
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
                take_profit_price=None,
                stop_loss_price=exit_plan.stop_loss_price,
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
            stop_loss_price=exit_plan.stop_loss_price,
        )
        logging.info("Swap %s %s submitted with attached SL and dynamic exit management: %s", action_label, position_side, order)

    def close_swap_position(self, symbol: str, signal: Signal, order_side: str) -> None:
        self.close_swap_position_fraction(symbol, signal.price, self.config.sell_fraction, f"{signal.reason} close")

    def close_swap_position_fraction(self, symbol: str, price: Decimal, fraction: Decimal, reason: str) -> bool:
        position_side = self.state.get_position_side(symbol)
        if position_side not in {"long", "short"}:
            logging.info("Swap close skipped because no local %s position is recorded.", symbol)
            return False
        order_side = "buy" if position_side == "short" else "sell"
        amount_contracts = self.state.get_position_base(symbol)
        amount_contracts = self.quantize_amount(amount_contracts * fraction)
        if amount_contracts >= self.state.get_position_base(symbol):
            amount_contracts = self.state.get_position_base(symbol)
        if amount_contracts <= 0:
            logging.info("Swap close skipped because no local %s position is recorded.", symbol)
            return False

        if self.config.dry_run:
            quote_notional = self.contract_notional(symbol, amount_contracts, price)
            realized_pnl = self.calculate_swap_pnl(symbol, amount_contracts, price)
            logging.info("[DRY RUN] Would close %s %s contracts=%s price=%s realized_pnl=%s reason=%s.", symbol, position_side, amount_contracts, price, realized_pnl, reason)
            self.state.record_trade(
                order_side,
                amount_contracts,
                price,
                quote_notional,
                mode="dry-run",
                symbol=symbol,
                position_side=position_side,
                reduce_only=True,
                realized_pnl=realized_pnl,
            )
            return True

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
            return False

        average_price = self.decimal_from_order(order, "average", price)
        close_price = average_price or price
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
        logging.info("Swap %s close submitted for %s: %s", position_side, reason, order)
        return True

    def build_swap_position_plan(
        self,
        symbol: str,
        entry_price: Decimal,
        size_multiplier: Decimal = Decimal("1"),
        stop_loss_pct: Decimal | None = None,
    ) -> SwapPositionPlan:
        if entry_price <= 0:
            raise RiskError("Entry price must be greater than 0.")
        if size_multiplier <= 0:
            raise RiskError("Position size multiplier must be greater than 0.")

        equity = self.fetch_account_equity()
        if equity <= 0:
            raise RiskError("Account equity is unavailable or zero.")

        risk_amount = equity * self.config.risk_per_trade_pct * size_multiplier
        stop_loss_pct = stop_loss_pct or self.config.stop_loss_pct_for_symbol(symbol)
        max_notional_by_risk = risk_amount / stop_loss_pct
        order_quote_amount = self.config.order_quote_amount * size_multiplier
        margin_budget = min(order_quote_amount, self.config.max_quote_per_order, max_notional_by_risk)
        if margin_budget <= 0:
            raise RiskError("Margin budget is zero.")

        needed_leverage = self.ceil_decimal(max_notional_by_risk / margin_budget)
        max_allowed_leverage = self.max_allowed_leverage(symbol)
        leverage = max(1, min(needed_leverage, max_allowed_leverage))
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

    @staticmethod
    def signal_risk_multiplier(signal: Signal) -> Decimal:
        raw_value = signal.indicators.get("risk_multiplier", 1)
        try:
            multiplier = Decimal(str(raw_value))
        except Exception:
            return Decimal("1")
        if multiplier <= 0:
            return Decimal("1")
        return min(multiplier, Decimal("1"))

    def max_allowed_leverage(self, symbol: str) -> int:
        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        max_leverage = self.decimal_from_path(market, ("limits", "leverage", "max"))
        if max_leverage is None:
            max_leverage = self.decimal_from_path(market, ("info", "lever"))
        if max_leverage is None:
            return 125
        return max(1, int(max_leverage))

    def create_swap_market_order_with_tp_sl(
        self,
        symbol: str,
        amount_contracts: Decimal,
        entry_price: Decimal,
        order_side: str,
        position_side: str,
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
    ) -> dict[str, Any]:
        if take_profit_price is None and stop_loss_price is None:
            take_profit_price, stop_loss_price = self.exit_prices(entry_price, position_side, symbol)
        params = self.swap_order_params(position_side)
        if self.config.attach_tp_sl:
            if take_profit_price is not None:
                params["takeProfit"] = {
                    "triggerPrice": float(take_profit_price),
                    "type": "market",
                    "triggerPriceType": "last",
                }
            if stop_loss_price is not None:
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

    def assert_loss_streak_limit_not_hit(self) -> None:
        max_losses = self.config.max_consecutive_daily_losses
        if max_losses > 0 and self.state.daily_loss_streak >= max_losses:
            raise RiskError(
                f"Daily consecutive loss limit reached: {self.state.daily_loss_streak} >= {max_losses}"
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
        position_mode = self.effective_position_mode()
        if position_mode == "hedge":
            params["posSide"] = position_side
        elif self.config.margin_mode == "isolated":
            params["posSide"] = "net"
        self.exchange.set_leverage(
            leverage,
            symbol,
            params=params,
        )

    def swap_order_params(self, position_side: str | None = None, reduce_only: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"tdMode": self.config.margin_mode}
        if reduce_only:
            params["reduceOnly"] = True
        position_mode = self.effective_position_mode()
        if position_mode == "hedge" and position_side in {"long", "short"}:
            params["positionSide"] = position_side
        elif position_mode == "net":
            params["positionSide"] = "net"
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

    def position_profit_pct(self, symbol: str, current_price: Decimal) -> Decimal:
        entry_price = self.state.get_entry_price(symbol)
        position_side = self.state.get_position_side(symbol)
        if entry_price <= 0:
            return Decimal("0")
        if position_side == "short":
            return (entry_price - current_price) / entry_price
        return (current_price - entry_price) / entry_price

    def add_position_trend_clear(self, candles: list[Candle], position_side: str) -> tuple[bool, dict[str, float]]:
        period = self.config.add_position_pullback_ma_period
        latest = candles[-1]
        current_ma = self.average_decimal([candle.close for candle in candles[-period:]])
        previous_ma = self.average_decimal([candle.close for candle in candles[-period - 1 : -1]])
        if position_side == "short":
            trend_clear = latest.close < current_ma and current_ma <= previous_ma
        else:
            trend_clear = latest.close > current_ma and current_ma >= previous_ma
        return trend_clear, {
            "add_trend_clear": 1.0 if trend_clear else 0.0,
            "add_ma": float(current_ma),
            "add_previous_ma": float(previous_ma),
        }

    def add_position_breakout(self, candles: list[Candle], position_side: str) -> tuple[bool, dict[str, float]]:
        lookback = self.config.add_position_breakout_lookback
        latest = candles[-1]
        previous = candles[-lookback - 1 : -1]
        avg_volume = self.average_decimal([candle.volume for candle in previous])
        volume_ok = latest.volume >= avg_volume * self.config.add_position_volume_multiplier if avg_volume > 0 else latest.volume > 0

        if position_side == "short":
            level = min(candle.low for candle in previous)
            breakout = latest.close < level and volume_ok
        else:
            level = max(candle.high for candle in previous)
            breakout = latest.close > level and volume_ok

        return breakout, {
            "add_breakout": 1.0 if breakout else 0.0,
            "add_breakout_level": float(level),
            "add_volume_ok": 1.0 if volume_ok else 0.0,
            "add_avg_volume": float(avg_volume),
        }

    def add_position_pullback(self, candles: list[Candle], position_side: str) -> tuple[bool, dict[str, float]]:
        ma_period = self.config.add_position_pullback_ma_period
        support_lookback = self.config.add_position_support_lookback
        tolerance = self.config.add_position_support_tolerance_pct
        latest = candles[-1]
        previous = candles[-support_lookback - 1 : -1]
        current_ma = self.average_decimal([candle.close for candle in candles[-ma_period:]])

        if position_side == "short":
            resistance = max(candle.high for candle in previous)
            ma_bounce = latest.high >= current_ma * (Decimal("1") - tolerance) and latest.close <= current_ma and latest.close <= latest.open
            support_bounce = latest.high >= resistance * (Decimal("1") - tolerance) and latest.close <= resistance and latest.close <= latest.open
            level = resistance
        else:
            support = min(candle.low for candle in previous)
            ma_bounce = latest.low <= current_ma * (Decimal("1") + tolerance) and latest.close >= current_ma and latest.close >= latest.open
            support_bounce = latest.low <= support * (Decimal("1") + tolerance) and latest.close >= support and latest.close >= latest.open
            level = support

        pullback = ma_bounce or support_bounce
        return pullback, {
            "add_pullback": 1.0 if pullback else 0.0,
            "add_ma_bounce": 1.0 if ma_bounce else 0.0,
            "add_support_bounce": 1.0 if support_bounce else 0.0,
            "add_support_level": float(level),
        }

    def close_action_for_symbol(self, symbol: str) -> str:
        return "buy" if self.state.get_position_side(symbol) == "short" else "sell"

    def exit_prices(self, entry_price: Decimal, position_side: str = "long", symbol: str | None = None) -> tuple[Decimal, Decimal]:
        stop_loss_pct = self.config.stop_loss_pct_for_symbol(symbol) if symbol else self.config.stop_loss_pct
        take_profit_pct = self.config.take_profit_pct_for_symbol(symbol) if symbol else self.config.take_profit_pct
        if position_side == "short":
            take_profit_price = entry_price * (Decimal("1") - take_profit_pct)
            stop_loss_price = entry_price * (Decimal("1") + stop_loss_pct)
            return take_profit_price, stop_loss_price
        take_profit_price = entry_price * (Decimal("1") + take_profit_pct)
        stop_loss_price = entry_price * (Decimal("1") - stop_loss_pct)
        return take_profit_price, stop_loss_price

    @staticmethod
    def price_at_r(position_side: str, entry_price: Decimal, risk_per_unit: Decimal, reward_risk: Decimal) -> Decimal:
        if position_side == "short":
            return entry_price - (risk_per_unit * reward_risk)
        return entry_price + (risk_per_unit * reward_risk)

    @staticmethod
    def average_true_range(candles: list[Candle]) -> Decimal:
        if len(candles) < 2:
            return Decimal("0")
        ranges: list[Decimal] = []
        previous_close = candles[0].close
        for candle in candles[1:]:
            ranges.append(max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            ))
            previous_close = candle.close
        if not ranges:
            return Decimal("0")
        return sum(ranges, Decimal("0")) / Decimal(len(ranges))

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

    def effective_position_mode(self) -> str:
        configured_mode = self.config.position_mode
        if configured_mode in {"net", "hedge"} and (self.config.dry_run or not self.config.has_private_credentials):
            return configured_mode

        cached_mode = getattr(self, "_effective_position_mode", None)
        if cached_mode in {"net", "hedge"}:
            return cached_mode

        if self.config.dry_run or not self.config.has_private_credentials:
            mode = "net" if configured_mode == "auto" else configured_mode
            self._effective_position_mode = mode
            return mode

        try:
            position_mode = self.exchange.fetch_position_mode()
        except Exception as exc:
            mode = "net" if configured_mode == "auto" else configured_mode
            logging.warning("Could not detect OKX position mode; using POSITION_MODE=%s. Error: %s", mode, exc)
            self._effective_position_mode = mode
            return mode

        detected_mode = "hedge" if position_mode.get("hedged") else "net"
        if configured_mode in {"net", "hedge"} and configured_mode != detected_mode:
            logging.warning(
                "Configured POSITION_MODE=%s but OKX account reports %s; using OKX account mode to avoid posSide errors.",
                configured_mode,
                detected_mode,
            )
        self._effective_position_mode = detected_mode
        return detected_mode

    @staticmethod
    def decimal_from_order(order: Any, key: str, fallback: Decimal) -> Decimal:
        if isinstance(order, dict) and order.get(key) not in (None, ""):
            return Decimal(str(order[key]))
        return fallback

    @staticmethod
    def decimal_from_path(source: Any, path: tuple[str, ...]) -> Decimal | None:
        current = source
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        if current in (None, ""):
            return None
        return Decimal(str(current))

    @staticmethod
    def quantize_amount(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    @staticmethod
    def ceil_decimal(value: Decimal) -> int:
        rounded = int(value)
        return rounded if value == Decimal(rounded) else rounded + 1

    @staticmethod
    def average_decimal(values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        return sum(values, Decimal("0")) / Decimal(len(values))

    @staticmethod
    def clamp_decimal(value: Decimal) -> Decimal:
        return max(Decimal("0"), min(Decimal("1"), value))

    @staticmethod
    def add_optional_indicator(indicators: dict[str, float], key: str, value: Decimal | None) -> None:
        if value is not None:
            indicators[key] = float(value)

    @staticmethod
    def format_confidence(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"

    @staticmethod
    def format_percent(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
