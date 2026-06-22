from __future__ import annotations

from decimal import Decimal

from .config import BotConfig
from .indicators import ema, rsi
from .models import Candle, Signal


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

