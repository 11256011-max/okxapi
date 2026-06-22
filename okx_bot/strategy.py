from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import BotConfig
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


@dataclass(frozen=True)
class VolumeProfile:
    poc: Decimal
    value_area_low: Decimal
    value_area_high: Decimal


@dataclass(frozen=True)
class ComponentScores:
    bullish: Decimal
    bearish: Decimal


def create_strategy(config: BotConfig) -> "CombinedMarketStructureStrategy":
    return CombinedMarketStructureStrategy(config)


class CombinedMarketStructureStrategy:
    """Order flow, liquidity sweep, anchored VWAP, volume profile, and SMC model.

    Confidence is a weighted composite of all five modules. It should be read as
    a signal-strength estimate, not a guaranteed win rate.
    """

    WEIGHTS = {
        "order_flow": Decimal("0.18"),
        "liquidity_sweep": Decimal("0.20"),
        "anchored_vwap": Decimal("0.20"),
        "volume_profile": Decimal("0.17"),
        "smc": Decimal("0.25"),
    }

    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def generate(self, candles: list[Candle]) -> Signal:
        current_price = candles[-1].close if candles else Decimal("0")
        minimum_candles = self.minimum_candles()
        if len(candles) < minimum_candles:
            return Signal(
                "hold",
                f"Not enough candles for combined strategy ({len(candles)} < {minimum_candles}).",
                current_price,
            )

        latest = candles[-1]
        previous = candles[-2]
        structure_window = candles[-self.config.combined_structure_lookback - 1 : -1]
        recent_high = max(candle.high for candle in structure_window)
        recent_low = min(candle.low for candle in structure_window)

        order_flow = self.order_flow_scores(candles)
        liquidity = self.liquidity_sweep_scores(latest, recent_high, recent_low)
        avwap, avwap_indicators = self.anchored_vwap_scores(candles)
        profile, profile_indicators = self.volume_profile_scores(candles)
        smc, smc_indicators = self.smc_scores(candles, previous, latest, recent_high, recent_low)

        bullish_score = self.weighted_score(
            order_flow.bullish,
            liquidity.bullish,
            avwap.bullish,
            profile.bullish,
            smc.bullish,
        )
        bearish_score = self.weighted_score(
            order_flow.bearish,
            liquidity.bearish,
            avwap.bearish,
            profile.bearish,
            smc.bearish,
        )

        edge = abs(bullish_score - bearish_score)
        indicators = {
            "order_flow_bullish_score": float(order_flow.bullish),
            "order_flow_bearish_score": float(order_flow.bearish),
            "liquidity_sweep_bullish_score": float(liquidity.bullish),
            "liquidity_sweep_bearish_score": float(liquidity.bearish),
            "anchored_vwap_bullish_score": float(avwap.bullish),
            "anchored_vwap_bearish_score": float(avwap.bearish),
            "volume_profile_bullish_score": float(profile.bullish),
            "volume_profile_bearish_score": float(profile.bearish),
            "smc_bullish_score": float(smc.bullish),
            "smc_bearish_score": float(smc.bearish),
            "bullish_score": float(bullish_score),
            "bearish_score": float(bearish_score),
            "strategy_edge": float(edge),
            "integrated_strategy_confidence": float(max(bullish_score, bearish_score)),
            "recent_high": float(recent_high),
            "recent_low": float(recent_low),
            **avwap_indicators,
            **profile_indicators,
            **smc_indicators,
        }

        if bullish_score >= self.config.combined_min_score and edge >= self.config.combined_min_edge and bullish_score > bearish_score:
            reason = self.reason("long", bullish_score, edge, indicators)
            return Signal("buy", reason, current_price, {**indicators, "confidence": float(bullish_score)}, bullish_score)

        if bearish_score >= self.config.combined_min_score and edge >= self.config.combined_min_edge and bearish_score > bullish_score:
            reason = self.reason("short", bearish_score, edge, indicators)
            return Signal("sell", reason, current_price, {**indicators, "confidence": float(bearish_score)}, bearish_score)

        confidence = max(bullish_score, bearish_score)
        reason = (
            "Combined strategy score did not reach threshold or directional edge. "
            f"bullish={self.format_percent(bullish_score)}, bearish={self.format_percent(bearish_score)}, "
            f"edge={self.format_percent(edge)}."
        )
        return Signal("hold", reason, current_price, {**indicators, "confidence": float(confidence)}, confidence)

    def order_flow_scores(self, candles: list[Candle]) -> ComponentScores:
        latest = candles[-1]
        previous = candles[-self.config.combined_order_flow_lookback - 1 : -1]
        avg_volume = self.average([candle.volume for candle in previous])
        candle_range = latest.high - latest.low
        if candle_range <= 0:
            return ComponentScores(Decimal("0"), Decimal("0"))

        body_ratio = (latest.close - latest.open) / candle_range
        close_location = (latest.close - latest.low) / candle_range
        volume_ratio = latest.volume / avg_volume if avg_volume > 0 else Decimal("1")
        volume_score = self.clamp(volume_ratio / Decimal("1.5"))

        bullish = Decimal("0")
        bearish = Decimal("0")
        if body_ratio > 0:
            bullish = self.clamp((body_ratio * Decimal("0.65")) + (volume_score * Decimal("0.25")) + (close_location * Decimal("0.10")))
        elif body_ratio < 0:
            bearish_close_location = Decimal("1") - close_location
            bearish = self.clamp((abs(body_ratio) * Decimal("0.65")) + (volume_score * Decimal("0.25")) + (bearish_close_location * Decimal("0.10")))

        return ComponentScores(bullish, bearish)

    def liquidity_sweep_scores(self, latest: Candle, recent_high: Decimal, recent_low: Decimal) -> ComponentScores:
        tolerance = self.config.combined_sweep_tolerance_pct
        bullish_sweep = latest.low < recent_low * (Decimal("1") - tolerance) and latest.close > recent_low and latest.close > latest.open
        bearish_sweep = latest.high > recent_high * (Decimal("1") + tolerance) and latest.close < recent_high and latest.close < latest.open

        bullish = Decimal("1") if bullish_sweep else Decimal("0")
        bearish = Decimal("1") if bearish_sweep else Decimal("0")
        return ComponentScores(bullish, bearish)

    def anchored_vwap_scores(self, candles: list[Candle]) -> tuple[ComponentScores, dict[str, float]]:
        window_start = max(0, len(candles) - self.config.combined_avwap_lookback)
        window = candles[window_start:]
        latest = candles[-1]
        low_anchor_index = window_start + min(range(len(window)), key=lambda index: window[index].low)
        high_anchor_index = window_start + max(range(len(window)), key=lambda index: window[index].high)
        bullish_vwap = self.anchored_vwap(candles[low_anchor_index:])
        bearish_vwap = self.anchored_vwap(candles[high_anchor_index:])

        bullish = Decimal("0")
        bearish = Decimal("0")
        if bullish_vwap > 0 and latest.close > bullish_vwap:
            reclaim_bonus = Decimal("0.25") if candles[-2].close <= bullish_vwap or latest.low <= bullish_vwap else Decimal("0")
            bullish = self.clamp(Decimal("0.75") + reclaim_bonus)
        if bearish_vwap > 0 and latest.close < bearish_vwap:
            reject_bonus = Decimal("0.25") if candles[-2].close >= bearish_vwap or latest.high >= bearish_vwap else Decimal("0")
            bearish = self.clamp(Decimal("0.75") + reject_bonus)

        indicators = {
            "anchored_vwap_from_low": float(bullish_vwap),
            "anchored_vwap_from_high": float(bearish_vwap),
        }
        return ComponentScores(bullish, bearish), indicators

    def volume_profile_scores(self, candles: list[Candle]) -> tuple[ComponentScores, dict[str, float]]:
        profile_window = candles[-self.config.combined_volume_profile_lookback :]
        profile = self.build_volume_profile(profile_window)
        latest = candles[-1]

        bullish = Decimal("0")
        bearish = Decimal("0")
        if latest.close > profile.poc:
            bullish = Decimal("0.65")
            if latest.close > profile.value_area_high:
                bullish += Decimal("0.20")
            if latest.low <= profile.value_area_low <= latest.close:
                bullish += Decimal("0.15")
        if latest.close < profile.poc:
            bearish = Decimal("0.65")
            if latest.close < profile.value_area_low:
                bearish += Decimal("0.20")
            if latest.high >= profile.value_area_high >= latest.close:
                bearish += Decimal("0.15")

        indicators = {
            "volume_profile_poc": float(profile.poc),
            "volume_profile_value_area_low": float(profile.value_area_low),
            "volume_profile_value_area_high": float(profile.value_area_high),
        }
        return ComponentScores(self.clamp(bullish), self.clamp(bearish)), indicators

    def smc_scores(
        self,
        candles: list[Candle],
        previous: Candle,
        latest: Candle,
        recent_high: Decimal,
        recent_low: Decimal,
    ) -> tuple[ComponentScores, dict[str, float]]:
        swing_highs, swing_lows = self.find_swings(candles)
        last_high = swing_highs[-1].price if swing_highs else recent_high
        previous_high = swing_highs[-2].price if len(swing_highs) >= 2 else recent_high
        last_low = swing_lows[-1].price if swing_lows else recent_low
        previous_low = swing_lows[-2].price if len(swing_lows) >= 2 else recent_low

        bullish_structure = last_high >= previous_high and last_low >= previous_low and latest.close > previous.close
        bearish_structure = last_high <= previous_high and last_low <= previous_low and latest.close < previous.close
        bullish_bos = previous.close <= last_high < latest.close or latest.close > recent_high
        bearish_bos = previous.close >= last_low > latest.close or latest.close < recent_low
        bullish_displacement = self.has_displacement(latest.close, last_high)
        bearish_displacement = self.has_displacement(last_low, latest.close)
        bullish_fvg = self.has_recent_fvg(candles, "bullish")
        bearish_fvg = self.has_recent_fvg(candles, "bearish")
        bullish_order_block = self.find_order_block(candles, "bullish")
        bearish_order_block = self.find_order_block(candles, "bearish")

        bullish = Decimal("0")
        bullish += Decimal("0.35") if bullish_bos else Decimal("0")
        bullish += Decimal("0.20") if bullish_structure else Decimal("0")
        bullish += Decimal("0.15") if bullish_displacement else Decimal("0")
        bullish += Decimal("0.15") if bullish_fvg else Decimal("0")
        bullish += Decimal("0.15") if bullish_order_block is not None else Decimal("0")

        bearish = Decimal("0")
        bearish += Decimal("0.35") if bearish_bos else Decimal("0")
        bearish += Decimal("0.20") if bearish_structure else Decimal("0")
        bearish += Decimal("0.15") if bearish_displacement else Decimal("0")
        bearish += Decimal("0.15") if bearish_fvg else Decimal("0")
        bearish += Decimal("0.15") if bearish_order_block is not None else Decimal("0")

        indicators = {
            "last_swing_high": float(last_high),
            "last_swing_low": float(last_low),
            "bullish_structure": 1.0 if bullish_structure else 0.0,
            "bearish_structure": 1.0 if bearish_structure else 0.0,
            "bullish_bos": 1.0 if bullish_bos else 0.0,
            "bearish_bos": 1.0 if bearish_bos else 0.0,
            "bullish_fvg": 1.0 if bullish_fvg else 0.0,
            "bearish_fvg": 1.0 if bearish_fvg else 0.0,
            "bullish_order_block_bottom": float(bullish_order_block.bottom) if bullish_order_block else 0.0,
            "bullish_order_block_top": float(bullish_order_block.top) if bullish_order_block else 0.0,
            "bearish_order_block_bottom": float(bearish_order_block.bottom) if bearish_order_block else 0.0,
            "bearish_order_block_top": float(bearish_order_block.top) if bearish_order_block else 0.0,
        }
        return ComponentScores(self.clamp(bullish), self.clamp(bearish)), indicators

    def weighted_score(
        self,
        order_flow: Decimal,
        liquidity: Decimal,
        avwap: Decimal,
        profile: Decimal,
        smc: Decimal,
    ) -> Decimal:
        return self.clamp(
            (order_flow * self.WEIGHTS["order_flow"])
            + (liquidity * self.WEIGHTS["liquidity_sweep"])
            + (avwap * self.WEIGHTS["anchored_vwap"])
            + (profile * self.WEIGHTS["volume_profile"])
            + (smc * self.WEIGHTS["smc"])
        )

    def build_volume_profile(self, candles: list[Candle]) -> VolumeProfile:
        low = min(candle.low for candle in candles)
        high = max(candle.high for candle in candles)
        bins = self.config.combined_volume_profile_bins
        if high <= low:
            return VolumeProfile(candles[-1].close, low, high)

        step = (high - low) / Decimal(bins)
        volumes = [Decimal("0") for _ in range(bins)]
        for candle in candles:
            typical_price = (candle.high + candle.low + candle.close) / Decimal("3")
            index = int((typical_price - low) / step)
            index = max(0, min(bins - 1, index))
            volumes[index] += candle.volume

        poc_index = max(range(bins), key=lambda index: volumes[index])
        total_volume = sum(volumes, Decimal("0"))
        target_volume = total_volume * self.config.combined_value_area_pct
        low_index = poc_index
        high_index = poc_index
        accumulated = volumes[poc_index]

        while accumulated < target_volume and (low_index > 0 or high_index < bins - 1):
            left_volume = volumes[low_index - 1] if low_index > 0 else Decimal("-1")
            right_volume = volumes[high_index + 1] if high_index < bins - 1 else Decimal("-1")
            if right_volume >= left_volume and high_index < bins - 1:
                high_index += 1
                accumulated += volumes[high_index]
            elif low_index > 0:
                low_index -= 1
                accumulated += volumes[low_index]
            else:
                break

        poc = low + (step * (Decimal(poc_index) + Decimal("0.5")))
        value_area_low = low + (step * Decimal(low_index))
        value_area_high = low + (step * Decimal(high_index + 1))
        return VolumeProfile(poc, value_area_low, value_area_high)

    def anchored_vwap(self, candles: list[Candle]) -> Decimal:
        total_volume = sum((candle.volume for candle in candles), Decimal("0"))
        if total_volume <= 0:
            return Decimal("0")
        total = Decimal("0")
        for candle in candles:
            typical_price = (candle.high + candle.low + candle.close) / Decimal("3")
            total += typical_price * candle.volume
        return total / total_volume

    def find_swings(self, candles: list[Candle]) -> tuple[list[SwingPoint], list[SwingPoint]]:
        lookback = self.config.combined_swing_lookback
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

    def find_order_block(self, candles: list[Candle], side: str) -> PriceZone | None:
        start = max(0, len(candles) - self.config.combined_structure_lookback - 1)
        for index in range(len(candles) - 2, start, -1):
            candle = candles[index]
            next_candle = candles[index + 1]
            if side == "bullish" and candle.close < candle.open and next_candle.close > next_candle.open:
                return PriceZone(bottom=candle.low, top=max(candle.open, candle.close), index=index)
            if side == "bearish" and candle.close > candle.open and next_candle.close < next_candle.open:
                return PriceZone(bottom=min(candle.open, candle.close), top=candle.high, index=index)
        return None

    def has_recent_fvg(self, candles: list[Candle], side: str) -> bool:
        start = max(2, len(candles) - self.config.combined_structure_lookback)
        for index in range(start, len(candles)):
            if side == "bullish" and candles[index - 2].high < candles[index].low:
                return True
            if side == "bearish" and candles[index - 2].low > candles[index].high:
                return True
        return False

    def has_displacement(self, higher_price: Decimal, lower_price: Decimal) -> bool:
        if lower_price <= 0:
            return False
        move_pct = abs(higher_price - lower_price) / lower_price
        return move_pct >= self.config.combined_min_displacement_pct

    def minimum_candles(self) -> int:
        return max(
            self.config.combined_structure_lookback,
            self.config.combined_volume_profile_lookback,
            self.config.combined_avwap_lookback,
            self.config.combined_order_flow_lookback,
            (self.config.combined_swing_lookback * 2) + 5,
        ) + 2

    def reason(self, side: str, confidence: Decimal, edge: Decimal, indicators: dict[str, float]) -> str:
        suffix = "long" if side == "long" else "short"
        aligned = [
            "order flow" if indicators[f"order_flow_{'bullish' if side == 'long' else 'bearish'}_score"] >= 0.5 else "",
            "liquidity sweep" if indicators[f"liquidity_sweep_{'bullish' if side == 'long' else 'bearish'}_score"] >= 0.5 else "",
            "anchored VWAP" if indicators[f"anchored_vwap_{'bullish' if side == 'long' else 'bearish'}_score"] >= 0.5 else "",
            "volume profile" if indicators[f"volume_profile_{'bullish' if side == 'long' else 'bearish'}_score"] >= 0.5 else "",
            "SMC" if indicators[f"smc_{'bullish' if side == 'long' else 'bearish'}_score"] >= 0.5 else "",
        ]
        modules = ", ".join(item for item in aligned if item) or "mixed modules"
        return (
            f"Combined {suffix} setup confirmed by {modules}. "
            f"Integrated confidence={self.format_percent(confidence)}, edge={self.format_percent(edge)}."
        )

    @staticmethod
    def average(values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        return sum(values, Decimal("0")) / Decimal(len(values))

    @staticmethod
    def clamp(value: Decimal) -> Decimal:
        return max(Decimal("0"), min(Decimal("1"), value))

    @staticmethod
    def format_percent(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"

    @staticmethod
    def is_unique_high(high: Decimal, candles: list[Candle]) -> bool:
        return sum(1 for candle in candles if candle.high == high) == 1

    @staticmethod
    def is_unique_low(low: Decimal, candles: list[Candle]) -> bool:
        return sum(1 for candle in candles if candle.low == low) == 1
