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


@dataclass(frozen=True)
class TimeframeEvaluation:
    current_price: Decimal
    bullish_score: Decimal
    bearish_score: Decimal
    edge: Decimal
    indicators: dict[str, float]
    ready: bool = True
    reason: str = ""


@dataclass(frozen=True)
class MarketState:
    direction: str
    trend_clear: bool
    low_volatility: bool
    ranging: bool
    atr_pct: Decimal
    ma_slope_pct: Decimal
    range_pct: Decimal
    current_ma: Decimal
    previous_ma: Decimal


@dataclass(frozen=True)
class LayeredEntrySetup:
    valid: bool
    confidence: Decimal
    reason: str
    indicators: dict[str, float]


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
        evaluation = self.evaluate_timeframe(candles)
        if not evaluation.ready:
            return Signal("hold", evaluation.reason, evaluation.current_price, evaluation.indicators)
        return self.signal_from_evaluation(evaluation, "Combined")

    def generate_multi(self, candles_by_timeframe: dict[str, list[Candle]]) -> Signal:
        missing = [
            timeframe
            for timeframe in self.config.analysis_timeframes
            if timeframe not in candles_by_timeframe
        ]
        if missing:
            return Signal("hold", f"Missing timeframe candles: {', '.join(missing)}.", Decimal("0"))

        evaluations: dict[str, TimeframeEvaluation] = {}
        for timeframe in self.config.analysis_timeframes:
            evaluation = self.evaluate_timeframe(candles_by_timeframe[timeframe])
            if not evaluation.ready:
                return Signal(
                    "hold",
                    f"{timeframe} not ready for multi-timeframe strategy. {evaluation.reason}",
                    evaluation.current_price,
                    self.prefix_indicators(timeframe, evaluation.indicators),
                )
            evaluations[timeframe] = evaluation

        if self.config.layered_smc_enabled:
            return self.generate_layered_smc(candles_by_timeframe, evaluations)

        entry_timeframe = self.config.entry_timeframe
        entry_evaluation = evaluations[entry_timeframe]
        confirmation_timeframes = [
            timeframe
            for timeframe in self.config.confirmation_timeframes
            if timeframe in evaluations and timeframe != entry_timeframe
        ]

        bullish_score = entry_evaluation.bullish_score
        bearish_score = entry_evaluation.bearish_score
        edge = entry_evaluation.edge
        bullish_aligned = all(
            evaluations[timeframe].bullish_score >= evaluations[timeframe].bearish_score
            for timeframe in confirmation_timeframes
        )
        bearish_aligned = all(
            evaluations[timeframe].bearish_score >= evaluations[timeframe].bullish_score
            for timeframe in confirmation_timeframes
        )

        indicators: dict[str, float] = {
            "bullish_score": float(bullish_score),
            "bearish_score": float(bearish_score),
            "strategy_edge": float(edge),
            "integrated_strategy_confidence": float(max(bullish_score, bearish_score)),
            "entry_timeframe_filter_mode": 1.0,
            "higher_timeframe_bullish_alignment": 1.0 if bullish_aligned else 0.0,
            "higher_timeframe_bearish_alignment": 1.0 if bearish_aligned else 0.0,
        }
        for timeframe, evaluation in evaluations.items():
            indicators.update(self.prefix_indicators(timeframe, evaluation.indicators))

        entry_bullish = entry_evaluation.bullish_score > entry_evaluation.bearish_score
        entry_bearish = entry_evaluation.bearish_score > entry_evaluation.bullish_score

        if (
            bullish_score >= self.config.combined_min_score
            and edge >= self.config.combined_min_edge
            and bullish_score > bearish_score
            and entry_bullish
            and bullish_aligned
        ):
            reason = self.multi_timeframe_reason("long", bullish_score, edge, confirmation_timeframes)
            return Signal("buy", reason, entry_evaluation.current_price, {**indicators, "confidence": float(bullish_score)}, bullish_score)

        if (
            bearish_score >= self.config.combined_min_score
            and edge >= self.config.combined_min_edge
            and bearish_score > bullish_score
            and entry_bearish
            and bearish_aligned
        ):
            reason = self.multi_timeframe_reason("short", bearish_score, edge, confirmation_timeframes)
            return Signal("sell", reason, entry_evaluation.current_price, {**indicators, "confidence": float(bearish_score)}, bearish_score)

        confidence = max(bullish_score, bearish_score)
        reason = (
            f"{entry_timeframe} entry strategy did not reach threshold, directional edge, or higher-timeframe alignment. "
            f"bullish={self.format_percent(bullish_score)}, bearish={self.format_percent(bearish_score)}, "
            f"edge={self.format_percent(edge)}. Higher-timeframe filters={', '.join(confirmation_timeframes) or 'none'}."
        )
        return Signal("hold", reason, entry_evaluation.current_price, {**indicators, "confidence": float(confidence)}, confidence)

    def generate_layered_smc(
        self,
        candles_by_timeframe: dict[str, list[Candle]],
        evaluations: dict[str, TimeframeEvaluation],
    ) -> Signal:
        entry_timeframe = self.config.entry_timeframe
        entry_evaluation = evaluations[entry_timeframe]
        market_timeframe = "4h" if "4h" in evaluations else self.config.confirmation_timeframes[-1]
        market = self.market_state(candles_by_timeframe[market_timeframe])
        indicators = self.layered_base_indicators(evaluations, market_timeframe, market)

        if not market.trend_clear:
            confidence = max(entry_evaluation.bullish_score, entry_evaluation.bearish_score)
            reason = (
                f"Layered SMC hold: {market_timeframe} market state is not tradable "
                f"(direction={market.direction}, slope={self.format_percent(abs(market.ma_slope_pct))}, "
                f"ATR={self.format_percent(market.atr_pct)})."
            )
            return Signal("hold", reason, entry_evaluation.current_price, {**indicators, "confidence": float(confidence)}, confidence)

        side = market.direction
        if side not in {"long", "short"}:
            confidence = max(entry_evaluation.bullish_score, entry_evaluation.bearish_score)
            reason = f"Layered SMC hold: {market_timeframe} trend direction is neutral."
            return Signal("hold", reason, entry_evaluation.current_price, {**indicators, "confidence": float(confidence)}, confidence)

        aligned, alignment_reason, alignment_indicators = self.layered_direction_alignment(side, evaluations)
        indicators.update(alignment_indicators)
        if not aligned:
            confidence = max(entry_evaluation.bullish_score, entry_evaluation.bearish_score)
            reason = f"Layered SMC hold: {alignment_reason}"
            return Signal("hold", reason, entry_evaluation.current_price, {**indicators, "confidence": float(confidence)}, confidence)

        setup = self.layered_entry_setup(entry_evaluation, side)
        indicators.update(setup.indicators)
        indicators["confidence"] = float(setup.confidence)
        if not setup.valid:
            reason = f"Layered SMC hold: 30m entry not confirmed. {setup.reason}"
            return Signal("hold", reason, entry_evaluation.current_price, indicators, setup.confidence)

        action = "buy" if side == "long" else "sell"
        reason = (
            f"Layered SMC {side} setup: {market_timeframe} trend is tradable, "
            f"higher timeframes align, and {entry_timeframe} entry confirms SMC/liquidity/order flow/value area. "
            f"{setup.reason}"
        )
        return Signal(action, reason, entry_evaluation.current_price, indicators, setup.confidence)

    def layered_base_indicators(
        self,
        evaluations: dict[str, TimeframeEvaluation],
        market_timeframe: str,
        market: MarketState,
    ) -> dict[str, float]:
        indicators: dict[str, float] = {
            "layered_smc_mode": 1.0,
            "market_direction": self.direction_value(market.direction),
            "market_trend_clear": 1.0 if market.trend_clear else 0.0,
            "market_low_volatility": 1.0 if market.low_volatility else 0.0,
            "market_ranging": 1.0 if market.ranging else 0.0,
            "market_atr_pct": float(market.atr_pct),
            "market_ma_slope_pct": float(market.ma_slope_pct),
            "market_range_pct": float(market.range_pct),
            "market_current_ma": float(market.current_ma),
            "market_previous_ma": float(market.previous_ma),
            "market_timeframe_4h": 1.0 if market_timeframe == "4h" else 0.0,
        }
        for timeframe, evaluation in evaluations.items():
            indicators.update(self.prefix_indicators(timeframe, evaluation.indicators))
        return indicators

    def market_state(self, candles: list[Candle]) -> MarketState:
        latest = candles[-1]
        period = self.config.layered_market_ma_period
        slope_lookback = self.config.layered_market_slope_lookback
        current_ma = self.average([candle.close for candle in candles[-period:]])
        previous_ma = self.average([candle.close for candle in candles[-period - slope_lookback : -slope_lookback]])
        ma_slope_pct = (current_ma - previous_ma) / latest.close if latest.close > 0 else Decimal("0")
        atr = self.average_true_range(candles, self.config.dynamic_exit_atr_period)
        atr_pct = atr / latest.close if latest.close > 0 else Decimal("0")
        range_window = candles[-period:]
        range_pct = (max(candle.high for candle in range_window) - min(candle.low for candle in range_window)) / latest.close if latest.close > 0 else Decimal("0")

        if latest.close > current_ma and current_ma > previous_ma:
            direction = "long"
        elif latest.close < current_ma and current_ma < previous_ma:
            direction = "short"
        else:
            direction = "neutral"

        low_volatility = atr_pct < self.config.layered_market_min_atr_pct
        slope_clear = abs(ma_slope_pct) >= self.config.layered_market_min_trend_pct
        ranging = direction == "neutral" or (not slope_clear and range_pct <= self.config.layered_market_max_range_pct)
        trend_clear = direction in {"long", "short"} and slope_clear and not low_volatility and not ranging
        return MarketState(
            direction=direction,
            trend_clear=trend_clear,
            low_volatility=low_volatility,
            ranging=ranging,
            atr_pct=atr_pct,
            ma_slope_pct=ma_slope_pct,
            range_pct=range_pct,
            current_ma=current_ma,
            previous_ma=previous_ma,
        )

    def layered_direction_alignment(
        self,
        side: str,
        evaluations: dict[str, TimeframeEvaluation],
    ) -> tuple[bool, str, dict[str, float]]:
        indicators: dict[str, float] = {}
        expected = "bullish" if side == "long" else "bearish"
        mismatches: list[str] = []
        for timeframe in self.config.confirmation_timeframes:
            evaluation = evaluations.get(timeframe)
            if evaluation is None:
                continue
            direction = self.evaluation_direction(evaluation)
            indicators[f"{timeframe}_direction"] = self.direction_value("long" if direction == "bullish" else "short" if direction == "bearish" else "neutral")
            indicators[f"{timeframe}_{expected}_direction_confirmed"] = 1.0 if direction == expected else 0.0
            if direction != expected:
                mismatches.append(f"{timeframe}={direction}")

        if mismatches:
            side_label = "bullish" if side == "long" else "bearish"
            return False, f"4H/1H direction must both be {side_label}; mismatches: {', '.join(mismatches)}.", indicators
        return True, "higher timeframes aligned.", indicators

    def layered_entry_setup(self, evaluation: TimeframeEvaluation, side: str) -> LayeredEntrySetup:
        direction_key = "bullish" if side == "long" else "bearish"
        opposite_key = "bearish" if side == "long" else "bullish"
        smc_score = self.indicator_decimal(evaluation, f"smc_{direction_key}_score")
        opposite_smc_score = self.indicator_decimal(evaluation, f"smc_{opposite_key}_score")
        bos_score = self.indicator_decimal(evaluation, f"{direction_key}_bos")
        liquidity_score = self.indicator_decimal(evaluation, f"liquidity_sweep_{direction_key}_score")
        order_flow_score = self.indicator_decimal(evaluation, f"order_flow_{direction_key}_score")
        avwap_score = self.indicator_decimal(evaluation, f"anchored_vwap_{direction_key}_score")
        profile_score = self.indicator_decimal(evaluation, f"volume_profile_{direction_key}_score")
        value_area_score = max(avwap_score, profile_score)

        bos_ok = bos_score >= Decimal("1") or not self.config.layered_entry_require_bos
        smc_score_ok = smc_score >= self.config.layered_entry_smc_min_score
        smc_direction_ok = smc_score >= opposite_smc_score
        smc_ok = smc_score_ok and smc_direction_ok and bos_ok
        liquidity_ok = liquidity_score >= Decimal("1") if self.config.layered_require_liquidity_sweep else True
        order_flow_ok = order_flow_score >= self.config.layered_entry_order_flow_min_score
        value_ok = value_area_score >= self.config.layered_entry_position_min_score
        optional_bonus = (
            (liquidity_score * Decimal("0.08"))
            + (order_flow_score * Decimal("0.08"))
            + (value_area_score * Decimal("0.08"))
        )
        conflict_penalty = opposite_smc_score * Decimal("0.15")
        confidence = self.clamp(
            smc_score
            + optional_bonus
            - conflict_penalty
        )
        score_ok = confidence >= self.config.combined_min_score

        blockers = [
            "SMC below main setup threshold" if not smc_score_ok else "",
            "opposite SMC score is stronger" if not smc_direction_ok else "",
            "SMC BOS missing" if not bos_ok else "",
            "required liquidity sweep missing" if not liquidity_ok else "",
            f"confidence below COMBINED_MIN_SCORE {self.format_percent(self.config.combined_min_score)}" if not score_ok else "",
        ]
        confirmations = [
            "liquidity sweep" if liquidity_score > Decimal("0") else "",
            "order flow" if order_flow_ok else "",
            "VWAP/volume profile" if value_ok else "",
        ]
        confirmation_text = ", ".join(item for item in confirmations if item) or "SMC-only"
        reason = (
            f"SMC={self.format_percent(smc_score)}, liquidity={self.format_percent(liquidity_score)}, "
            f"order_flow={self.format_percent(order_flow_score)}, value_area={self.format_percent(value_area_score)}, "
            f"optional_confirmations={confirmation_text}, layered_confidence={self.format_percent(confidence)}."
        )
        blocker_text = "; ".join(item for item in blockers if item)
        if blocker_text:
            reason = f"{reason} Blockers: {blocker_text}."

        indicators = {
            f"layered_{direction_key}_smc_score": float(smc_score),
            f"layered_{direction_key}_bos": float(bos_score),
            f"layered_{direction_key}_liquidity_score": float(liquidity_score),
            f"layered_{direction_key}_order_flow_score": float(order_flow_score),
            f"layered_{direction_key}_value_area_score": float(value_area_score),
            "layered_entry_smc_ok": 1.0 if smc_ok else 0.0,
            "layered_entry_bos_ok": 1.0 if bos_ok else 0.0,
            "layered_entry_liquidity_ok": 1.0 if liquidity_ok else 0.0,
            "layered_entry_order_flow_ok": 1.0 if order_flow_ok else 0.0,
            "layered_entry_value_area_ok": 1.0 if value_ok else 0.0,
            "layered_entry_score_ok": 1.0 if score_ok else 0.0,
            "layered_optional_confirmation_count": float(sum(1 for item in confirmations if item)),
            "layered_optional_bonus": float(optional_bonus),
            "layered_conflict_penalty": float(conflict_penalty),
            "layered_entry_confidence": float(confidence),
        }
        return LayeredEntrySetup(smc_ok and liquidity_ok and score_ok, confidence, reason, indicators)

    def evaluate_timeframe(self, candles: list[Candle]) -> TimeframeEvaluation:
        current_price = candles[-1].close if candles else Decimal("0")
        minimum_candles = self.minimum_candles()
        if len(candles) < minimum_candles:
            return TimeframeEvaluation(
                current_price,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                {},
                ready=False,
                reason=f"Not enough candles for combined strategy ({len(candles)} < {minimum_candles}).",
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
        return TimeframeEvaluation(current_price, bullish_score, bearish_score, edge, indicators)

    def signal_from_evaluation(self, evaluation: TimeframeEvaluation, label: str) -> Signal:
        if (
            evaluation.bullish_score >= self.config.combined_min_score
            and evaluation.edge >= self.config.combined_min_edge
            and evaluation.bullish_score > evaluation.bearish_score
        ):
            reason = self.reason("long", evaluation.bullish_score, evaluation.edge, evaluation.indicators, label)
            return Signal(
                "buy",
                reason,
                evaluation.current_price,
                {**evaluation.indicators, "confidence": float(evaluation.bullish_score)},
                evaluation.bullish_score,
            )

        if (
            evaluation.bearish_score >= self.config.combined_min_score
            and evaluation.edge >= self.config.combined_min_edge
            and evaluation.bearish_score > evaluation.bullish_score
        ):
            reason = self.reason("short", evaluation.bearish_score, evaluation.edge, evaluation.indicators, label)
            return Signal(
                "sell",
                reason,
                evaluation.current_price,
                {**evaluation.indicators, "confidence": float(evaluation.bearish_score)},
                evaluation.bearish_score,
            )

        confidence = max(evaluation.bullish_score, evaluation.bearish_score)
        reason = (
            f"{label} strategy score did not reach threshold or directional edge. "
            f"bullish={self.format_percent(evaluation.bullish_score)}, bearish={self.format_percent(evaluation.bearish_score)}, "
            f"edge={self.format_percent(evaluation.edge)}."
        )
        return Signal("hold", reason, evaluation.current_price, {**evaluation.indicators, "confidence": float(confidence)}, confidence)

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

    def reason(self, side: str, confidence: Decimal, edge: Decimal, indicators: dict[str, float], label: str = "Combined") -> str:
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
            f"{label} {suffix} setup confirmed by {modules}. "
            f"Integrated confidence={self.format_percent(confidence)}, edge={self.format_percent(edge)}."
        )

    def multi_timeframe_reason(self, side: str, confidence: Decimal, edge: Decimal, confirmation_timeframes: list[str]) -> str:
        suffix = "long" if side == "long" else "short"
        confirmations = ", ".join(confirmation_timeframes) if confirmation_timeframes else "no higher timeframe"
        return (
            f"{self.config.entry_timeframe} {suffix} entry confirmed by higher-timeframe direction filter ({confirmations}). "
            f"Entry confidence={self.format_percent(confidence)}, edge={self.format_percent(edge)}."
        )

    def evaluation_direction(self, evaluation: TimeframeEvaluation) -> str:
        bullish_smc = self.indicator_decimal(evaluation, "smc_bullish_score")
        bearish_smc = self.indicator_decimal(evaluation, "smc_bearish_score")
        if evaluation.bullish_score > evaluation.bearish_score and bullish_smc >= bearish_smc:
            return "bullish"
        if evaluation.bearish_score > evaluation.bullish_score and bearish_smc >= bullish_smc:
            return "bearish"
        return "neutral"

    def indicator_decimal(self, evaluation: TimeframeEvaluation, key: str) -> Decimal:
        return Decimal(str(evaluation.indicators.get(key, 0)))

    def average_true_range(self, candles: list[Candle], period: int) -> Decimal:
        if len(candles) < period + 1:
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
        return self.average(ranges)

    @staticmethod
    def prefix_indicators(timeframe: str, indicators: dict[str, float]) -> dict[str, float]:
        safe_timeframe = timeframe.replace("/", "_").replace(":", "_")
        return {
            f"{safe_timeframe}_{key}": value
            for key, value in indicators.items()
        }

    @staticmethod
    def direction_value(direction: str) -> float:
        if direction in {"long", "bullish"}:
            return 1.0
        if direction in {"short", "bearish"}:
            return -1.0
        return 0.0

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
