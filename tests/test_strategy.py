from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig
from okx_bot.models import Candle
from okx_bot.strategy import CombinedMarketStructureStrategy, MarketState, TimeframeEvaluation, create_strategy


def candle(index: int, open_: str, high: str, low: str, close: str, volume: str = "10") -> Candle:
    return Candle(
        timestamp=index,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def config(extra_env: dict[str, str] | None = None) -> BotConfig:
    env = {
        "STRATEGY": "combined",
        "CANDLE_LIMIT": "100",
        "COMBINED_STRUCTURE_LOOKBACK": "30",
        "COMBINED_ORDER_FLOW_LOOKBACK": "20",
        "COMBINED_AVWAP_LOOKBACK": "40",
        "COMBINED_VOLUME_PROFILE_LOOKBACK": "40",
        "COMBINED_MIN_SCORE": "0.68",
        "COMBINED_MIN_EDGE": "0.12",
        "ENTRY_TIMEFRAME": "30m",
        "CONFIRMATION_TIMEFRAMES": "1h,4h",
        "EXTERNAL_CONTEXT_ENABLED": "false",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        bot_config = BotConfig.from_env()
        bot_config.validate()
        return bot_config


def range_candles(count: int = 80) -> list[Candle]:
    candles: list[Candle] = []
    for index in range(count):
        base = Decimal("100") + (Decimal(index % 8) * Decimal("0.25"))
        open_ = base - Decimal("0.10")
        close = base + Decimal("0.05")
        candles.append(
            candle(
                index,
                str(open_),
                str(base + Decimal("0.45")),
                str(base - Decimal("0.45")),
                str(close),
                "10",
            )
        )
    return candles


class StrategyTests(unittest.TestCase):
    def test_create_strategy_selects_combined(self) -> None:
        self.assertIsInstance(create_strategy(config()), CombinedMarketStructureStrategy)

    def test_legacy_smc_name_maps_to_combined(self) -> None:
        self.assertEqual(config({"STRATEGY": "smc"}).strategy, "combined")

    def test_combined_generates_buy_when_all_modules_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        candles = range_candles()
        candles.append(candle(80, "100.2", "105.2", "98.4", "104.7", "35"))

        signal = strategy.generate(candles)

        self.assertEqual(signal.action, "buy")
        self.assertGreaterEqual(signal.confidence, Decimal("0.80"))
        self.assertGreater(signal.indicators["bullish_score"], signal.indicators["bearish_score"])
        self.assertGreaterEqual(signal.indicators["liquidity_sweep_bullish_score"], 1.0)
        self.assertGreaterEqual(signal.indicators["order_flow_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["anchored_vwap_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["volume_profile_bullish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["smc_bullish_score"], 0.5)

    def test_combined_generates_sell_when_all_modules_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        candles = range_candles()
        candles.append(candle(80, "101.6", "103.9", "96.2", "97.0", "35"))

        signal = strategy.generate(candles)

        self.assertEqual(signal.action, "sell")
        self.assertGreaterEqual(signal.confidence, Decimal("0.80"))
        self.assertGreater(signal.indicators["bearish_score"], signal.indicators["bullish_score"])
        self.assertGreaterEqual(signal.indicators["liquidity_sweep_bearish_score"], 1.0)
        self.assertGreaterEqual(signal.indicators["order_flow_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["anchored_vwap_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["volume_profile_bearish_score"], 0.5)
        self.assertGreaterEqual(signal.indicators["smc_bearish_score"], 0.5)

    def test_combined_holds_when_composite_score_is_too_low(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        signal = strategy.generate(range_candles())

        self.assertEqual(signal.action, "hold")
        self.assertLess(signal.confidence, Decimal("0.80"))

    def test_multi_timeframe_generates_buy_when_entry_and_higher_timeframes_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        buy_candles = range_candles()
        buy_candles.append(candle(80, "100.2", "105.2", "98.4", "104.7", "35"))

        signal = strategy.generate_multi({
            "30m": buy_candles,
            "1h": buy_candles,
            "4h": buy_candles,
        })

        self.assertEqual(signal.action, "buy")
        self.assertGreaterEqual(signal.confidence, Decimal("0.80"))
        self.assertEqual(signal.indicators["higher_timeframe_bullish_alignment"], 1.0)
        self.assertIn("30m long entry", signal.reason)

    def test_multi_timeframe_holds_when_higher_timeframe_disagrees(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        buy_candles = range_candles()
        buy_candles.append(candle(80, "100.2", "105.2", "98.4", "104.7", "35"))
        sell_candles = range_candles()
        sell_candles.append(candle(80, "101.6", "103.9", "96.2", "97.0", "35"))

        signal = strategy.generate_multi({
            "30m": buy_candles,
            "1h": buy_candles,
            "4h": sell_candles,
        })

        self.assertEqual(signal.action, "hold")
        self.assertEqual(signal.indicators["higher_timeframe_bullish_alignment"], 0.0)

    def test_multi_timeframe_uses_entry_score_and_filters_higher_timeframes(self) -> None:
        strategy = CombinedMarketStructureStrategy(config())
        entry = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.70"),
            Decimal("0.55"),
            Decimal("0.15"),
            {"bullish_score": 0.70, "bearish_score": 0.55},
        )
        weak_bullish_confirmation = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.05"),
            Decimal("0.04"),
            Decimal("0.01"),
            {"bullish_score": 0.05, "bearish_score": 0.04},
        )

        with patch.object(
            strategy,
            "evaluate_timeframe",
            side_effect=[entry, weak_bullish_confirmation, weak_bullish_confirmation],
        ):
            signal = strategy.generate_multi({
                "30m": range_candles(),
                "1h": range_candles(),
                "4h": range_candles(),
            })

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.confidence, Decimal("0.70"))
        self.assertEqual(signal.indicators["bullish_score"], 0.70)
        self.assertEqual(signal.indicators["higher_timeframe_bullish_alignment"], 1.0)

    def test_layered_smc_generates_buy_only_after_market_and_entry_layers_align(self) -> None:
        strategy = CombinedMarketStructureStrategy(config({"LAYERED_SMC_ENABLED": "true"}))
        entry = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.74"),
            Decimal("0.20"),
            Decimal("0.54"),
            {
                "smc_bullish_score": 0.80,
                "smc_bearish_score": 0.10,
                "bullish_bos": 1.0,
                "liquidity_sweep_bullish_score": 1.0,
                "order_flow_bullish_score": 0.60,
                "anchored_vwap_bullish_score": 0.70,
                "volume_profile_bullish_score": 0.20,
            },
        )
        confirmation = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.60"),
            Decimal("0.20"),
            Decimal("0.40"),
            {"smc_bullish_score": 0.60, "smc_bearish_score": 0.10},
        )
        tradable_market = MarketState(
            direction="long",
            trend_clear=True,
            low_volatility=False,
            ranging=False,
            atr_pct=Decimal("0.01"),
            ma_slope_pct=Decimal("0.01"),
            range_pct=Decimal("0.04"),
            current_ma=Decimal("101"),
            previous_ma=Decimal("100"),
        )

        with patch.object(strategy, "evaluate_timeframe", side_effect=[entry, confirmation, confirmation]), patch.object(
            strategy,
            "market_state",
            return_value=tradable_market,
        ):
            signal = strategy.generate_multi({
                "30m": range_candles(),
                "1h": range_candles(),
                "4h": range_candles(),
            })

        self.assertEqual(signal.action, "buy")
        self.assertGreaterEqual(signal.confidence, Decimal("0.68"))
        self.assertEqual(signal.indicators["layered_smc_mode"], 1.0)
        self.assertEqual(signal.indicators["layered_entry_liquidity_ok"], 1.0)
        self.assertIn("Layered SMC long setup", signal.reason)

    def test_layered_smc_holds_when_four_hour_market_is_not_tradable(self) -> None:
        strategy = CombinedMarketStructureStrategy(config({"LAYERED_SMC_ENABLED": "true"}))
        evaluation = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.80"),
            Decimal("0.10"),
            Decimal("0.70"),
            {"smc_bullish_score": 0.80, "smc_bearish_score": 0.10},
        )
        range_market = MarketState(
            direction="neutral",
            trend_clear=False,
            low_volatility=True,
            ranging=True,
            atr_pct=Decimal("0.001"),
            ma_slope_pct=Decimal("0.0001"),
            range_pct=Decimal("0.005"),
            current_ma=Decimal("100"),
            previous_ma=Decimal("100"),
        )

        with patch.object(strategy, "evaluate_timeframe", side_effect=[evaluation, evaluation, evaluation]), patch.object(
            strategy,
            "market_state",
            return_value=range_market,
        ):
            signal = strategy.generate_multi({
                "30m": range_candles(),
                "1h": range_candles(),
                "4h": range_candles(),
            })

        self.assertEqual(signal.action, "hold")
        self.assertEqual(signal.indicators["market_trend_clear"], 0.0)
        self.assertIn("market state is not tradable", signal.reason)

    def test_layered_smc_allows_smc_only_entry_without_optional_confirmations(self) -> None:
        strategy = CombinedMarketStructureStrategy(config({"LAYERED_SMC_ENABLED": "true"}))
        entry = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.72"),
            Decimal("0.10"),
            Decimal("0.62"),
            {
                "smc_bullish_score": 0.72,
                "smc_bearish_score": 0.00,
                "bullish_bos": 1.0,
                "liquidity_sweep_bullish_score": 0.0,
                "order_flow_bullish_score": 0.0,
                "anchored_vwap_bullish_score": 0.0,
                "volume_profile_bullish_score": 0.0,
            },
        )
        confirmation = TimeframeEvaluation(
            Decimal("100"),
            Decimal("0.60"),
            Decimal("0.20"),
            Decimal("0.40"),
            {"smc_bullish_score": 0.60, "smc_bearish_score": 0.10},
        )
        tradable_market = MarketState(
            direction="long",
            trend_clear=True,
            low_volatility=False,
            ranging=False,
            atr_pct=Decimal("0.01"),
            ma_slope_pct=Decimal("0.01"),
            range_pct=Decimal("0.04"),
            current_ma=Decimal("101"),
            previous_ma=Decimal("100"),
        )

        with patch.object(strategy, "evaluate_timeframe", side_effect=[entry, confirmation, confirmation]), patch.object(
            strategy,
            "market_state",
            return_value=tradable_market,
        ):
            signal = strategy.generate_multi({
                "30m": range_candles(),
                "1h": range_candles(),
                "4h": range_candles(),
            })

        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.indicators["layered_optional_confirmation_count"], 0.0)
        self.assertIn("SMC-only", signal.reason)


if __name__ == "__main__":
    unittest.main()
