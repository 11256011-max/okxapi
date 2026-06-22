from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import BotConfig
from .indicators import ema, rsi
from .models import Candle, Signal


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: Decimal


@dataclass(frozen=True)
class PriceZone:
    bottom: Decimal
    top: Decimal
    index: int


def create_strategy(config: BotConfig) -> "EmaRsiStrategy | SmcStrategy":
    if config.strategy == "smc":
        return SmcStrategy(config)
    return EmaRsiStrategy(config)


class EmaRsiStrategy:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def generate(self, candles: list[Candle]) -> Signal:
        if len(candles) < max(self.config.slow_ema, self.config.rsi_period) + 5:
            return Signal("hold", "Not enough candles for indicators.", Decimal("0"))

        closes = [float(candle.close) for candle in candles]
        fast_values = ema(closes, self.config.fast_ema)
        slow_values = ema(closes, self.config.slow_ema)
        rsi_values = rsi(closes, self.config.rsi_period)

        current_price = candles[-1].close
        current_fast = fast_values[-1]
        current_slow = slow_values[-1]
        previous_fast = fast_values[-2]
        previous_slow = slow_values[-2]
        current_rsi = rsi_values[-1]

        indicators = {
            "fast_ema": current_fast,
            "slow_ema": current_slow,
            "rsi": float(current_rsi) if current_rsi is not None else 0.0,
        }

        if current_rsi is None:
            return Signal("hold", "RSI is not ready yet.", current_price, indicators)

        crossed_up = previous_fast <= previous_slow and current_fast > current_slow
        crossed_down = previous_fast >= previous_slow and current_fast < current_slow

        if crossed_up and Decimal(str(current_rsi)) <= self.config.rsi_buy_max:
            return Signal("buy", "Fast EMA crossed above slow EMA and RSI is acceptable.", current_price, indicators)

        if crossed_down:
            return Signal("sell", "Fast EMA crossed below slow EMA.", current_price, indicators)

        if Decimal(str(current_rsi)) >= self.config.rsi_sell_min:
            return Signal("sell", "RSI reached the configured sell threshold.", current_price, indicators)

        return Signal("hold", "No entry or exit signal.", current_price, indicators)


class SmcStrategy:
    """Rule-based Smart Money Concepts strategy for spot long-only trading.

    This is intentionally conservative: it looks for confirmed swing structure,
    break of structure, a recent bullish order block, and optionally a fair
    value gap. It does not attempt to short because this bot is spot-only.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def generate(self, candles: list[Candle]) -> Signal:
        current_price = candles[-1].close if candles else Decimal("0")
        minimum_candles = (self.config.smc_swing_lookback * 2) + self.config.smc_zone_lookback + 5
        if len(candles) < minimum_candles:
            return Signal("hold", "Not enough candles for SMC structure.", current_price)

        swing_highs, swing_lows = self.find_swings(candles)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return Signal("hold", "Not enough confirmed swing points for SMC.", current_price)

        previous_candle = candles[-2]
        latest_candle = candles[-1]
        last_high = swing_highs[-1]
        previous_high = swing_highs[-2]
        last_low = swing_lows[-1]
        previous_low = swing_lows[-2]

        bullish_structure = last_high.price > previous_high.price and last_low.price > previous_low.price
        bearish_structure = last_high.price < previous_high.price and last_low.price < previous_low.price
        bullish_bos = self.broke_above(previous_candle.close, latest_candle.close, last_high.price)
        bearish_bos = self.broke_below(previous_candle.close, latest_candle.close, last_low.price)
        bullish_displacement = self.has_displacement(latest_candle.close, last_high.price)
        bearish_displacement = self.has_displacement(last_low.price, latest_candle.close)
        bullish_fvg = self.has_recent_bullish_fvg(candles)
        bullish_order_block = self.find_bullish_order_block(candles)
        in_order_block = self.price_in_zone(current_price, bullish_order_block)

        indicators = {
            "last_swing_high": float(last_high.price),
            "last_swing_low": float(last_low.price),
            "bullish_structure": 1.0 if bullish_structure else 0.0,
            "bearish_structure": 1.0 if bearish_structure else 0.0,
            "bullish_bos": 1.0 if bullish_bos else 0.0,
            "bearish_bos": 1.0 if bearish_bos else 0.0,
            "bullish_fvg": 1.0 if bullish_fvg else 0.0,
            "order_block_bottom": float(bullish_order_block.bottom) if bullish_order_block else 0.0,
            "order_block_top": float(bullish_order_block.top) if bullish_order_block else 0.0,
        }

        if bearish_bos and bearish_displacement:
            return Signal("sell", "SMC bearish break of structure with displacement.", current_price, indicators)

        if bearish_structure and latest_candle.close < last_low.price:
            return Signal("sell", "SMC bearish market structure invalidated the long thesis.", current_price, indicators)

        fvg_ok = bullish_fvg or not self.config.smc_require_fvg
        if bullish_bos and bullish_displacement and fvg_ok:
            return Signal("buy", "SMC bullish break of structure with displacement.", current_price, indicators)

        if bullish_structure and bullish_order_block and in_order_block and fvg_ok and latest_candle.close > latest_candle.open:
            return Signal("buy", "SMC pullback into bullish order block with bullish structure.", current_price, indicators)

        return Signal("hold", "No SMC entry or exit signal.", current_price, indicators)

    def find_swings(self, candles: list[Candle]) -> tuple[list[SwingPoint], list[SwingPoint]]:
        lookback = self.config.smc_swing_lookback
        swing_highs: list[SwingPoint] = []
        swing_lows: list[SwingPoint] = []

        for index in range(lookback, len(candles) - lookback):
            window = candles[index - lookback : index + lookback + 1]
            high = candles[index].high
            low = candles[index].low
            if high == max(candle.high for candle in window) and self.is_unique_high(high, window):
                swing_highs.append(SwingPoint(index=index, price=high))
            if low == min(candle.low for candle in window) and self.is_unique_low(low, window):
                swing_lows.append(SwingPoint(index=index, price=low))

        return swing_highs, swing_lows

    def find_bullish_order_block(self, candles: list[Candle]) -> PriceZone | None:
        start = max(0, len(candles) - self.config.smc_zone_lookback - 1)
        for index in range(len(candles) - 2, start, -1):
            candle = candles[index]
            next_candle = candles[index + 1]
            if candle.close < candle.open and next_candle.close > next_candle.open:
                return PriceZone(
                    bottom=candle.low,
                    top=max(candle.open, candle.close),
                    index=index,
                )
        return None

    def has_recent_bullish_fvg(self, candles: list[Candle]) -> bool:
        start = max(2, len(candles) - self.config.smc_zone_lookback)
        for index in range(start, len(candles)):
            if candles[index - 2].high < candles[index].low:
                return True
        return False

    def price_in_zone(self, price: Decimal, zone: PriceZone | None) -> bool:
        if zone is None:
            return False
        tolerance = self.config.smc_zone_tolerance_pct
        lower = zone.bottom * (Decimal("1") - tolerance)
        upper = zone.top * (Decimal("1") + tolerance)
        return lower <= price <= upper

    def has_displacement(self, higher_price: Decimal, lower_price: Decimal) -> bool:
        if lower_price <= 0:
            return False
        move_pct = abs(higher_price - lower_price) / lower_price
        return move_pct >= self.config.smc_min_displacement_pct

    @staticmethod
    def broke_above(previous_close: Decimal, current_close: Decimal, level: Decimal) -> bool:
        return previous_close <= level < current_close

    @staticmethod
    def broke_below(previous_close: Decimal, current_close: Decimal, level: Decimal) -> bool:
        return previous_close >= level > current_close

    @staticmethod
    def is_unique_high(high: Decimal, candles: list[Candle]) -> bool:
        return sum(1 for candle in candles if candle.high == high) == 1

    @staticmethod
    def is_unique_low(low: Decimal, candles: list[Candle]) -> bool:
        return sum(1 for candle in candles if candle.low == low) == 1
