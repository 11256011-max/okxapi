from __future__ import annotations

import csv
import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import ccxt

from .config import BotConfig
from .exit_plan import build_exit_plan
from .models import Candle, Signal
from .strategy import create_strategy


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str
    signal_time: int
    entry_time: int
    exit_time: int
    entry_price: Decimal
    exit_price: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal
    result: str
    gross_pnl_pct: Decimal
    fee_cost_pct: Decimal
    slippage_cost_pct: Decimal
    funding_cost_pct: Decimal
    pnl_pct: Decimal
    notional: Decimal
    gross_pnl_quote: Decimal
    fee_cost_quote: Decimal
    slippage_cost_quote: Decimal
    funding_cost_quote: Decimal
    pnl_quote: Decimal
    account_return_pct: Decimal
    partial_exit_price: Decimal | None
    partial_fraction: Decimal
    confidence: Decimal
    reason: str

    @property
    def won(self) -> bool:
        return self.pnl_pct > 0


@dataclass
class OpenBacktestTrade:
    symbol: str
    side: str
    signal_time: int
    entry_time: int
    entry_index: int
    entry_price: Decimal
    take_profit_price: Decimal
    partial_take_profit_price: Decimal
    breakeven_price: Decimal
    initial_stop_loss_price: Decimal
    stop_loss_price: Decimal
    highest_price: Decimal
    lowest_price: Decimal
    risk_per_unit: Decimal
    notional: Decimal
    confidence: Decimal
    reason: str
    remaining_fraction: Decimal = Decimal("1")
    partial_realized_pnl_pct: Decimal = Decimal("0")
    partial_exit_price: Decimal | None = None
    breakeven_armed: bool = False
    partial_taken: bool = False


@dataclass(frozen=True)
class BacktestResult:
    trades: list[BacktestTrade]
    total_completed_trades: int
    starting_equity: Decimal

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.won)

    @property
    def losses(self) -> int:
        return len(self.trades) - self.wins

    @property
    def win_rate(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return Decimal(self.wins) / Decimal(len(self.trades))

    @property
    def average_pnl_pct(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum((trade.pnl_pct for trade in self.trades), Decimal("0")) / Decimal(len(self.trades))

    @property
    def average_gross_pnl_pct(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum((trade.gross_pnl_pct for trade in self.trades), Decimal("0")) / Decimal(len(self.trades))

    @property
    def average_cost_pct(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum((self.trade_cost_pct(trade) for trade in self.trades), Decimal("0")) / Decimal(len(self.trades))

    @property
    def total_pnl_quote(self) -> Decimal:
        return sum((trade.pnl_quote for trade in self.trades), Decimal("0"))

    @property
    def total_account_return_pct(self) -> Decimal:
        if self.starting_equity <= 0:
            return Decimal("0")
        return self.total_pnl_quote / self.starting_equity

    @property
    def average_account_return_pct(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum((trade.account_return_pct for trade in self.trades), Decimal("0")) / Decimal(len(self.trades))

    @property
    def average_notional(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return sum((trade.notional for trade in self.trades), Decimal("0")) / Decimal(len(self.trades))

    @staticmethod
    def trade_cost_pct(trade: BacktestTrade) -> Decimal:
        return trade.fee_cost_pct + trade.slippage_cost_pct + trade.funding_cost_pct


class BacktestRunner:
    """Public OHLCV strategy replay. This never submits orders."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.strategy = create_strategy(config)
        self.exchange = ccxt.okx({
            "enableRateLimit": True,
            "options": {"defaultType": config.market_type},
        })
        self._timeframe_ms: dict[str, int] = {}

    def run(self, days: int = 365, max_trades: int = 100, csv_path: str | None = None) -> BacktestResult:
        if days <= 0:
            raise ValueError("--days must be greater than 0")
        if max_trades <= 0:
            raise ValueError("--trades must be greater than 0")

        now = self.exchange.milliseconds()
        start_ms = now - (days * 24 * 60 * 60 * 1000)
        warmup_ms = max(self.timeframe_ms(timeframe) for timeframe in self.config.analysis_timeframes) * self.config.candle_limit
        fetch_since = start_ms - warmup_ms
        all_trades: list[BacktestTrade] = []

        logging.info(
            "Backtest starting: symbols=%s entry=%s confirmations=%s days=%s threshold=%s symbol_thresholds=%s min_score=%s min_edge=%s fee=%s slippage=%s funding_8h=%s",
            ",".join(self.config.symbols),
            self.config.entry_timeframe,
            ",".join(self.config.confirmation_timeframes),
            days,
            self.format_percent(self.config.signal_confidence_threshold),
            self.format_symbol_thresholds(),
            self.format_percent(self.config.combined_min_score),
            self.format_percent(self.config.combined_min_edge),
            self.format_percent(self.config.backtest_fee_pct),
            self.format_percent(self.config.backtest_slippage_pct),
            self.format_percent(self.config.backtest_funding_rate_8h),
        )

        for symbol in self.config.symbols:
            histories = self.fetch_histories(symbol, fetch_since, now)
            symbol_trades = self.simulate_symbol(symbol, histories, start_ms)
            logging.info("Backtest %s completed trades=%s", symbol, len(symbol_trades))
            all_trades.extend(symbol_trades)

        all_trades.sort(key=lambda trade: trade.entry_time)
        selected_trades = all_trades[-max_trades:]
        result = BacktestResult(selected_trades, total_completed_trades=len(all_trades), starting_equity=self.config.backtest_start_equity)
        self.log_result(result)
        if csv_path:
            self.write_csv(csv_path, selected_trades)
        return result

    def fetch_histories(self, symbol: str, since: int, now: int) -> dict[str, list[Candle]]:
        return {
            timeframe: self.fetch_ohlcv_history(symbol, timeframe, since, now)
            for timeframe in self.config.analysis_timeframes
        }

    def fetch_ohlcv_history(self, symbol: str, timeframe: str, since: int, now: int) -> list[Candle]:
        timeframe_ms = self.timeframe_ms(timeframe)
        cursor = since
        rows_by_timestamp: dict[int, list[Any]] = {}
        while cursor < now:
            batch = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=300)
            if not batch:
                break
            for row in batch:
                timestamp = int(row[0])
                if timestamp + timeframe_ms <= now:
                    rows_by_timestamp[timestamp] = row
            last_timestamp = int(batch[-1][0])
            next_cursor = last_timestamp + timeframe_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        candles = [
            Candle(
                timestamp=timestamp,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
            )
            for timestamp, row in sorted(rows_by_timestamp.items())
        ]
        if not candles:
            raise RuntimeError(f"No backtest candles returned for {symbol} {timeframe}.")
        logging.info("Fetched %s %s candles for %s", len(candles), timeframe, symbol)
        return candles

    def simulate_symbol(
        self,
        symbol: str,
        histories: dict[str, list[Candle]],
        start_ms: int,
    ) -> list[BacktestTrade]:
        entry_timeframe = self.config.entry_timeframe
        entry_candles = histories[entry_timeframe]
        timestamps = {
            timeframe: [candle.timestamp for candle in candles]
            for timeframe, candles in histories.items()
        }
        trades: list[BacktestTrade] = []
        open_trade: OpenBacktestTrade | None = None

        for index in range(self.config.candle_limit, len(entry_candles) - 1):
            current_candle = entry_candles[index]
            next_candle = entry_candles[index + 1]
            signal_time = current_candle.timestamp + self.timeframe_ms(entry_timeframe)

            if open_trade is not None and index >= open_trade.entry_index:
                recent_candles = entry_candles[max(0, index - self.config.dynamic_exit_atr_period) : index + 1]
                closed_trade = self.stop_trade_if_hit(open_trade, current_candle, recent_candles)
                if closed_trade is not None:
                    trades.append(closed_trade)
                    open_trade = None
                    continue

            candles_by_timeframe = self.closed_candle_slices(histories, timestamps, signal_time)
            if candles_by_timeframe is None:
                continue

            signal = self.strategy.generate_multi(candles_by_timeframe)
            signal = self.apply_signal_confidence_gate(symbol, signal)

            if open_trade is not None:
                if self.is_opposite_signal(open_trade, signal):
                    trades.append(self.close_trade(open_trade, next_candle.timestamp, next_candle.open, "opposite_signal"))
                    open_trade = None
                continue

            if signal_time < start_ms or signal.action not in {"buy", "sell"}:
                continue

            side = "long" if signal.action == "buy" else "short"
            entry_price = next_candle.open
            exit_plan = build_exit_plan(
                self.config,
                symbol,
                entry_price,
                side,
                signal,
                candles_by_timeframe[entry_timeframe],
            )
            risk_per_unit = abs(entry_price - exit_plan.stop_loss_price)
            if risk_per_unit <= 0:
                continue
            partial_take_profit_price = self.price_at_r(side, entry_price, risk_per_unit, self.config.exit_partial_take_profit_r)
            open_trade = OpenBacktestTrade(
                symbol=symbol,
                side=side,
                signal_time=signal_time,
                entry_time=next_candle.timestamp,
                entry_index=index + 1,
                entry_price=entry_price,
                take_profit_price=partial_take_profit_price,
                partial_take_profit_price=partial_take_profit_price,
                breakeven_price=entry_price,
                initial_stop_loss_price=exit_plan.stop_loss_price,
                stop_loss_price=exit_plan.stop_loss_price,
                highest_price=entry_price,
                lowest_price=entry_price,
                risk_per_unit=risk_per_unit,
                notional=self.notional_for_trade(risk_per_unit / entry_price),
                confidence=signal.confidence,
                reason=signal.reason,
            )

        return trades

    def closed_candle_slices(
        self,
        histories: dict[str, list[Candle]],
        timestamps: dict[str, list[int]],
        signal_time: int,
    ) -> dict[str, list[Candle]] | None:
        candles_by_timeframe: dict[str, list[Candle]] = {}
        for timeframe in self.config.analysis_timeframes:
            close_cutoff = signal_time - self.timeframe_ms(timeframe)
            closed_count = bisect_right(timestamps[timeframe], close_cutoff)
            if closed_count < self.config.candle_limit:
                return None
            candles_by_timeframe[timeframe] = histories[timeframe][closed_count - self.config.candle_limit : closed_count]
        return candles_by_timeframe

    def stop_trade_if_hit(self, trade: OpenBacktestTrade, candle: Candle, recent_candles: list[Candle]) -> BacktestTrade | None:
        if trade.side == "long":
            trade.highest_price = max(trade.highest_price, candle.high)
            if candle.low <= trade.stop_loss_price:
                return self.close_trade(trade, candle.timestamp, trade.stop_loss_price, "stop_loss")
            if candle.high >= self.price_at_r(trade.side, trade.entry_price, trade.risk_per_unit, self.config.exit_breakeven_r):
                trade.stop_loss_price = max(trade.stop_loss_price, trade.breakeven_price)
                trade.breakeven_armed = True
            if not trade.partial_taken and candle.high >= trade.partial_take_profit_price:
                self.take_partial_profit(trade)
            self.update_trailing_stop(trade, recent_candles)
            if (trade.breakeven_armed or trade.partial_taken) and candle.low <= trade.stop_loss_price:
                result = "trailing_stop" if trade.partial_taken else "breakeven_stop"
                return self.close_trade(trade, candle.timestamp, trade.stop_loss_price, result)
            return None

        trade.lowest_price = min(trade.lowest_price, candle.low)
        if candle.high >= trade.stop_loss_price:
            return self.close_trade(trade, candle.timestamp, trade.stop_loss_price, "stop_loss")
        if candle.low <= self.price_at_r(trade.side, trade.entry_price, trade.risk_per_unit, self.config.exit_breakeven_r):
            trade.stop_loss_price = min(trade.stop_loss_price, trade.breakeven_price)
            trade.breakeven_armed = True
        if not trade.partial_taken and candle.low <= trade.partial_take_profit_price:
            self.take_partial_profit(trade)
        self.update_trailing_stop(trade, recent_candles)
        if (trade.breakeven_armed or trade.partial_taken) and candle.high >= trade.stop_loss_price:
            result = "trailing_stop" if trade.partial_taken else "breakeven_stop"
            return self.close_trade(trade, candle.timestamp, trade.stop_loss_price, result)
        return None

    def take_partial_profit(self, trade: OpenBacktestTrade) -> None:
        partial_fraction = min(self.config.exit_partial_fraction, trade.remaining_fraction)
        if partial_fraction <= 0:
            return
        partial_gross = self.gross_pnl_pct(trade.side, trade.entry_price, trade.partial_take_profit_price)
        trade.partial_realized_pnl_pct += partial_gross * partial_fraction
        trade.remaining_fraction -= partial_fraction
        trade.partial_exit_price = trade.partial_take_profit_price
        trade.partial_taken = True

    def update_trailing_stop(self, trade: OpenBacktestTrade, recent_candles: list[Candle]) -> None:
        if not self.config.exit_trailing_enabled or not trade.partial_taken or not recent_candles:
            return
        atr = self.average_true_range(recent_candles)
        if atr <= 0:
            return
        trailing_distance = atr * self.config.exit_trailing_atr_multiplier
        if trade.side == "short":
            trade.stop_loss_price = min(trade.stop_loss_price, trade.lowest_price + trailing_distance)
            return
        trade.stop_loss_price = max(trade.stop_loss_price, trade.highest_price - trailing_distance)

    def close_trade(
        self,
        trade: OpenBacktestTrade,
        exit_time: int,
        exit_price: Decimal,
        result: str,
    ) -> BacktestTrade:
        remaining_gross_pnl_pct = self.gross_pnl_pct(trade.side, trade.entry_price, exit_price) * trade.remaining_fraction
        gross_pnl_pct = trade.partial_realized_pnl_pct + remaining_gross_pnl_pct
        fee_cost_pct = self.config.backtest_fee_pct * Decimal("2")
        slippage_cost_pct = self.config.backtest_slippage_pct * Decimal("2")
        funding_cost_pct = self.funding_cost_pct(trade.entry_time, exit_time)
        pnl_pct = gross_pnl_pct - fee_cost_pct - slippage_cost_pct - funding_cost_pct
        gross_pnl_quote = trade.notional * gross_pnl_pct
        fee_cost_quote = trade.notional * fee_cost_pct
        slippage_cost_quote = trade.notional * slippage_cost_pct
        funding_cost_quote = trade.notional * funding_cost_pct
        pnl_quote = trade.notional * pnl_pct
        account_return_pct = pnl_quote / self.config.backtest_start_equity
        return BacktestTrade(
            symbol=trade.symbol,
            side=trade.side,
            signal_time=trade.signal_time,
            entry_time=trade.entry_time,
            exit_time=exit_time,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            take_profit_price=trade.take_profit_price,
            stop_loss_price=trade.stop_loss_price,
            result=result,
            gross_pnl_pct=gross_pnl_pct,
            fee_cost_pct=fee_cost_pct,
            slippage_cost_pct=slippage_cost_pct,
            funding_cost_pct=funding_cost_pct,
            pnl_pct=pnl_pct,
            notional=trade.notional,
            gross_pnl_quote=gross_pnl_quote,
            fee_cost_quote=fee_cost_quote,
            slippage_cost_quote=slippage_cost_quote,
            funding_cost_quote=funding_cost_quote,
            pnl_quote=pnl_quote,
            account_return_pct=account_return_pct,
            partial_exit_price=trade.partial_exit_price,
            partial_fraction=Decimal("1") - trade.remaining_fraction,
            confidence=trade.confidence,
            reason=trade.reason,
        )

    def apply_signal_confidence_gate(self, symbol: str, signal: Signal) -> Signal:
        threshold = self.config.confidence_threshold_for_symbol_and_action(symbol, signal.action)
        if signal.action in {"buy", "sell"} and signal.confidence < threshold:
            return Signal("hold", signal.reason, signal.price, signal.indicators, signal.confidence)
        return signal

    @staticmethod
    def is_opposite_signal(trade: OpenBacktestTrade, signal: Signal) -> bool:
        return (trade.side == "long" and signal.action == "sell") or (trade.side == "short" and signal.action == "buy")

    @staticmethod
    def gross_pnl_pct(side: str, entry_price: Decimal, exit_price: Decimal) -> Decimal:
        if side == "short":
            return (entry_price - exit_price) / entry_price
        return (exit_price - entry_price) / entry_price

    def funding_cost_pct(self, entry_time: int, exit_time: int) -> Decimal:
        held_ms = max(0, exit_time - entry_time)
        intervals = Decimal(held_ms) / Decimal(8 * 60 * 60 * 1000)
        return self.config.backtest_funding_rate_8h * intervals

    def notional_for_trade(self, stop_loss_pct: Decimal) -> Decimal:
        if stop_loss_pct <= 0:
            return Decimal("0")
        risk_amount = self.config.backtest_start_equity * self.config.risk_per_trade_pct
        return risk_amount / stop_loss_pct

    @staticmethod
    def price_at_r(side: str, entry_price: Decimal, risk_per_unit: Decimal, reward_risk: Decimal) -> Decimal:
        if side == "short":
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

    def time_frame_seconds(self, timeframe: str) -> int:
        return int(self.exchange.parse_timeframe(timeframe))

    def timeframe_ms(self, timeframe: str) -> int:
        if timeframe not in self._timeframe_ms:
            self._timeframe_ms[timeframe] = self.time_frame_seconds(timeframe) * 1000
        return self._timeframe_ms[timeframe]

    def log_result(self, result: BacktestResult) -> None:
        logging.info(
            "Backtest summary: total_completed=%s reported=%s wins=%s losses=%s win_rate=%s average_gross_pnl=%s average_cost=%s average_net_pnl=%s average_notional=%s total_net_pnl=%s account_return=%s",
            result.total_completed_trades,
            len(result.trades),
            result.wins,
            result.losses,
            self.format_percent(result.win_rate),
            self.format_percent(result.average_gross_pnl_pct),
            self.format_percent(result.average_cost_pct),
            self.format_percent(result.average_pnl_pct),
            result.average_notional.quantize(Decimal("0.01")),
            result.total_pnl_quote.quantize(Decimal("0.01")),
            self.format_percent(result.total_account_return_pct),
        )
        for symbol in sorted({trade.symbol for trade in result.trades}):
            symbol_trades = [trade for trade in result.trades if trade.symbol == symbol]
            wins = sum(1 for trade in symbol_trades if trade.won)
            win_rate = Decimal(wins) / Decimal(len(symbol_trades))
            average_pnl = sum((trade.pnl_pct for trade in symbol_trades), Decimal("0")) / Decimal(len(symbol_trades))
            average_gross = sum((trade.gross_pnl_pct for trade in symbol_trades), Decimal("0")) / Decimal(len(symbol_trades))
            average_cost = sum((BacktestResult.trade_cost_pct(trade) for trade in symbol_trades), Decimal("0")) / Decimal(len(symbol_trades))
            symbol_pnl_quote = sum((trade.pnl_quote for trade in symbol_trades), Decimal("0"))
            symbol_account_return = symbol_pnl_quote / self.config.backtest_start_equity
            logging.info(
                "Backtest symbol summary: %s trades=%s wins=%s losses=%s win_rate=%s average_gross_pnl=%s average_cost=%s average_net_pnl=%s net_pnl=%s account_return=%s",
                symbol,
                len(symbol_trades),
                wins,
                len(symbol_trades) - wins,
                self.format_percent(win_rate),
                self.format_percent(average_gross),
                self.format_percent(average_cost),
                self.format_percent(average_pnl),
                symbol_pnl_quote.quantize(Decimal("0.01")),
                self.format_percent(symbol_account_return),
            )
        for number, trade in enumerate(result.trades, start=1):
            logging.info(
                "Trade %03d %s %s entry=%s exit=%s result=%s gross=%s cost=%s net=%s notional=%s pnl=%s account_return=%s confidence=%s entry_time=%s exit_time=%s",
                number,
                trade.symbol,
                trade.side,
                trade.entry_price,
                trade.exit_price,
                trade.result,
                self.format_percent(trade.gross_pnl_pct),
                self.format_percent(BacktestResult.trade_cost_pct(trade)),
                self.format_percent(trade.pnl_pct),
                trade.notional.quantize(Decimal("0.01")),
                trade.pnl_quote.quantize(Decimal("0.01")),
                self.format_percent(trade.account_return_pct),
                self.format_percent(trade.confidence),
                self.format_timestamp(trade.entry_time),
                self.format_timestamp(trade.exit_time),
            )

    def write_csv(self, csv_path: str, trades: list[BacktestTrade]) -> None:
        path = Path(csv_path)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "symbol",
                    "side",
                    "signal_time_utc",
                    "entry_time_utc",
                    "exit_time_utc",
                    "entry_price",
                    "exit_price",
                    "take_profit_price",
                    "stop_loss_price",
                    "result",
                    "gross_pnl_pct",
                    "fee_cost_pct",
                    "slippage_cost_pct",
                    "funding_cost_pct",
                    "total_cost_pct",
                    "pnl_pct",
                    "notional",
                    "gross_pnl_quote",
                    "fee_cost_quote",
                    "slippage_cost_quote",
                    "funding_cost_quote",
                    "pnl_quote",
                    "account_return_pct",
                    "partial_exit_price",
                    "partial_fraction",
                    "confidence",
                    "reason",
                ],
            )
            writer.writeheader()
            for trade in trades:
                writer.writerow({
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "signal_time_utc": self.format_timestamp(trade.signal_time),
                    "entry_time_utc": self.format_timestamp(trade.entry_time),
                    "exit_time_utc": self.format_timestamp(trade.exit_time),
                    "entry_price": str(trade.entry_price),
                    "exit_price": str(trade.exit_price),
                    "take_profit_price": str(trade.take_profit_price),
                    "stop_loss_price": str(trade.stop_loss_price),
                    "result": trade.result,
                    "gross_pnl_pct": str(trade.gross_pnl_pct),
                    "fee_cost_pct": str(trade.fee_cost_pct),
                    "slippage_cost_pct": str(trade.slippage_cost_pct),
                    "funding_cost_pct": str(trade.funding_cost_pct),
                    "total_cost_pct": str(BacktestResult.trade_cost_pct(trade)),
                    "pnl_pct": str(trade.pnl_pct),
                    "notional": str(trade.notional),
                    "gross_pnl_quote": str(trade.gross_pnl_quote),
                    "fee_cost_quote": str(trade.fee_cost_quote),
                    "slippage_cost_quote": str(trade.slippage_cost_quote),
                    "funding_cost_quote": str(trade.funding_cost_quote),
                    "pnl_quote": str(trade.pnl_quote),
                    "account_return_pct": str(trade.account_return_pct),
                    "partial_exit_price": str(trade.partial_exit_price or ""),
                    "partial_fraction": str(trade.partial_fraction),
                    "confidence": str(trade.confidence),
                    "reason": trade.reason,
                })
        logging.info("Backtest CSV written to %s", path)

    @staticmethod
    def format_timestamp(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def format_symbol_thresholds(self) -> str:
        if not self.config.symbol_confidence_thresholds:
            return "none"
        return ",".join(
            f"{symbol}:{self.format_percent(threshold)}"
            for symbol, threshold in sorted(self.config.symbol_confidence_thresholds.items())
        )

    @staticmethod
    def format_percent(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
