from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import BotConfig
from .models import Candle, Signal


@dataclass(frozen=True)
class ExitPlan:
    take_profit_price: Decimal
    stop_loss_price: Decimal
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    reward_risk: Decimal
    dynamic: bool


def build_exit_plan(
    config: BotConfig,
    symbol: str,
    entry_price: Decimal,
    position_side: str,
    signal: Signal | None = None,
    candles: list[Candle] | None = None,
) -> ExitPlan:
    fixed_stop_pct = config.stop_loss_pct_for_symbol(symbol)
    fixed_take_profit_pct = config.take_profit_pct_for_symbol(symbol)
    if not config.dynamic_exit_enabled or not candles or not dynamic_exit_enabled_for_symbol(config, symbol):
        return plan_from_percentages(entry_price, position_side, fixed_stop_pct, fixed_take_profit_pct, dynamic=False)

    if not is_strong_trend(config, position_side, signal, candles):
        return plan_from_percentages(entry_price, position_side, fixed_stop_pct, fixed_take_profit_pct, dynamic=False)

    atr_pct = average_true_range_pct(candles, entry_price, config.dynamic_exit_atr_period)
    structure_pct = structure_stop_pct(candles, entry_price, position_side, config.dynamic_exit_structure_lookback)
    volatility_stop_pct = atr_pct * config.dynamic_exit_atr_multiplier if atr_pct > 0 else Decimal("0")
    raw_stop_pct = max(volatility_stop_pct, structure_pct, fixed_stop_pct)
    stop_pct = clamp(raw_stop_pct, config.dynamic_exit_min_stop_pct, config.dynamic_exit_max_stop_pct)

    reward_risk = config.dynamic_exit_strong_rr
    take_profit_pct = stop_pct * reward_risk
    return plan_from_percentages(entry_price, position_side, stop_pct, take_profit_pct, reward_risk=reward_risk, dynamic=True)


def plan_from_percentages(
    entry_price: Decimal,
    position_side: str,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    reward_risk: Decimal | None = None,
    dynamic: bool = False,
) -> ExitPlan:
    if position_side == "short":
        take_profit_price = entry_price * (Decimal("1") - take_profit_pct)
        stop_loss_price = entry_price * (Decimal("1") + stop_loss_pct)
    else:
        take_profit_price = entry_price * (Decimal("1") + take_profit_pct)
        stop_loss_price = entry_price * (Decimal("1") - stop_loss_pct)

    rr = reward_risk if reward_risk is not None else take_profit_pct / stop_loss_pct
    return ExitPlan(
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        reward_risk=rr,
        dynamic=dynamic,
    )


def dynamic_exit_enabled_for_symbol(config: BotConfig, symbol: str) -> bool:
    if not config.dynamic_exit_symbols:
        return True
    enabled_symbols = {item.strip().upper() for item in config.dynamic_exit_symbols}
    return any(candidate in enabled_symbols for candidate in config.symbol_threshold_candidates(symbol))


def average_true_range_pct(candles: list[Candle], entry_price: Decimal, period: int) -> Decimal:
    if entry_price <= 0 or len(candles) < period + 1:
        return Decimal("0")
    ranges: list[Decimal] = []
    window = candles[-period:]
    previous_close = candles[-period - 1].close
    for candle in window:
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        ranges.append(true_range)
        previous_close = candle.close
    return (sum(ranges, Decimal("0")) / Decimal(len(ranges))) / entry_price


def structure_stop_pct(candles: list[Candle], entry_price: Decimal, position_side: str, lookback: int) -> Decimal:
    if entry_price <= 0 or len(candles) < lookback:
        return Decimal("0")
    window = candles[-lookback:]
    if position_side == "short":
        recent_high = max(candle.high for candle in window)
        distance = (recent_high - entry_price) / entry_price
    else:
        recent_low = min(candle.low for candle in window)
        distance = (entry_price - recent_low) / entry_price
    return max(Decimal("0"), distance)


def is_strong_trend(
    config: BotConfig,
    position_side: str,
    signal: Signal | None,
    candles: list[Candle],
) -> bool:
    if signal is None or signal.confidence < config.dynamic_exit_strong_confidence:
        return False
    if not higher_timeframe_aligned(position_side, signal):
        return False
    return moving_average_trend_aligned(config, position_side, candles)


def higher_timeframe_aligned(position_side: str, signal: Signal) -> bool:
    key = "higher_timeframe_bullish_alignment" if position_side == "long" else "higher_timeframe_bearish_alignment"
    value = signal.indicators.get(key)
    return value is None or value >= 1.0


def moving_average_trend_aligned(config: BotConfig, position_side: str, candles: list[Candle]) -> bool:
    period = config.dynamic_exit_trend_ma_period
    if len(candles) < period + 1:
        return False
    current_ma = average([candle.close for candle in candles[-period:]])
    previous_ma = average([candle.close for candle in candles[-period - 1 : -1]])
    latest = candles[-1]
    if position_side == "short":
        return latest.close < current_ma and current_ma <= previous_ma
    return latest.close > current_ma and current_ma >= previous_ma


def average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))
