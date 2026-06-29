from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
STRATEGY_ALIASES = {
    "ema_rsi": "combined",
    "smc": "combined",
    "order_flow": "combined",
    "combined_order_flow": "combined",
    "combined_market_structure": "combined",
}


class ConfigError(ValueError):
    """Raised when the bot configuration is unsafe or invalid."""


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number") from exc


def env_probability(name: str, default: str) -> Decimal:
    value = env_decimal(name, default)
    if value > Decimal("1") and value <= Decimal("100"):
        return value / Decimal("100")
    return value


def normalize_strategy(strategy: str) -> str:
    normalized = strategy.strip().lower()
    return STRATEGY_ALIASES.get(normalized, normalized)


def parse_score_value(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name} values must be decimal numbers") from exc
    if abs(value) > Decimal("1") and abs(value) <= Decimal("100"):
        return value / Decimal("100")
    return value


def env_score_map(name: str) -> dict[str, Decimal]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}

    values: dict[str, Decimal] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        if ":" not in item:
            raise ConfigError(f"{name} entries must look like BTC:0.2")
        key, value = item.split(":", 1)
        key = key.strip().upper()
        if not key:
            raise ConfigError(f"{name} contains an empty symbol key")
        values[key] = parse_score_value(name, value)
    return values


def normalize_symbol(symbol: str, market_type: str) -> str:
    if market_type == "swap" and ":" not in symbol:
        base_quote = symbol.split("/")
        if len(base_quote) == 2:
            return f"{symbol}:{base_quote[1]}"
    return symbol


@dataclass(frozen=True)
class BotConfig:
    api_key: str
    secret_key: str
    passphrase: str
    okx_simulated_trading: bool
    dry_run: bool
    enable_live_trading: bool
    symbols: list[str]
    timeframe: str
    entry_timeframe: str
    confirmation_timeframes: list[str]
    candle_limit: int
    poll_seconds: int
    market_type: str
    margin_mode: str
    position_mode: str
    risk_per_trade_pct: Decimal
    daily_max_loss_pct: Decimal
    max_consecutive_daily_losses: int
    strategy: str
    signal_confidence_threshold: Decimal
    long_confidence_threshold: Decimal
    short_confidence_threshold: Decimal
    symbol_confidence_thresholds: dict[str, Decimal]
    layered_smc_enabled: bool
    layered_market_ma_period: int
    layered_market_slope_lookback: int
    layered_market_min_trend_pct: Decimal
    layered_market_min_atr_pct: Decimal
    layered_market_max_range_pct: Decimal
    layered_entry_smc_min_score: Decimal
    layered_entry_require_bos: bool
    layered_entry_require_pullback: bool
    layered_entry_pullback_tolerance_pct: Decimal
    layered_entry_max_chase_pct: Decimal
    layered_entry_order_flow_min_score: Decimal
    layered_entry_position_min_score: Decimal
    layered_require_liquidity_sweep: bool
    combined_swing_lookback: int
    combined_structure_lookback: int
    combined_order_flow_lookback: int
    combined_avwap_lookback: int
    combined_volume_profile_lookback: int
    combined_volume_profile_bins: int
    combined_value_area_pct: Decimal
    combined_sweep_tolerance_pct: Decimal
    combined_min_displacement_pct: Decimal
    combined_min_score: Decimal
    combined_min_edge: Decimal
    fast_ema: int
    slow_ema: int
    rsi_period: int
    rsi_buy_max: Decimal
    rsi_sell_min: Decimal
    smc_swing_lookback: int
    smc_zone_lookback: int
    smc_zone_tolerance_pct: Decimal
    smc_min_displacement_pct: Decimal
    smc_require_fvg: bool
    external_context_enabled: bool
    newsapi_enabled: bool
    gdelt_enabled: bool
    fear_greed_enabled: bool
    fundamental_context_enabled: bool
    newsapi_api_key: str
    newsapi_page_size: int
    external_context_lookback_hours: int
    external_context_cache_seconds: int
    external_context_timeout_seconds: int
    external_context_max_confidence_adjustment: Decimal
    external_context_min_support: Decimal
    external_context_extreme_threshold: Decimal
    external_context_risk_multiplier: Decimal
    fear_greed_mode: str
    fundamental_bias: dict[str, Decimal]
    add_position_enabled: bool
    max_position_adds: int
    add_position_quote_fraction: Decimal
    add_position_require_profit: bool
    add_position_min_profit_pct: Decimal
    add_position_breakout_lookback: int
    add_position_pullback_ma_period: int
    add_position_support_lookback: int
    add_position_support_tolerance_pct: Decimal
    add_position_volume_multiplier: Decimal
    order_quote_amount: Decimal
    max_quote_per_order: Decimal
    stop_loss_pct: Decimal
    symbol_stop_loss_pcts: dict[str, Decimal]
    take_profit_pct: Decimal
    symbol_take_profit_pcts: dict[str, Decimal]
    attach_tp_sl: bool
    sell_fraction: Decimal
    dynamic_exit_enabled: bool
    dynamic_exit_symbols: list[str]
    dynamic_exit_atr_period: int
    dynamic_exit_structure_lookback: int
    dynamic_exit_min_stop_pct: Decimal
    dynamic_exit_max_stop_pct: Decimal
    dynamic_exit_atr_multiplier: Decimal
    dynamic_exit_base_rr: Decimal
    dynamic_exit_strong_rr: Decimal
    dynamic_exit_strong_confidence: Decimal
    dynamic_exit_trend_ma_period: int
    exit_breakeven_r: Decimal
    exit_partial_take_profit_r: Decimal
    exit_partial_fraction: Decimal
    exit_trailing_atr_multiplier: Decimal
    exit_trailing_enabled: bool
    backtest_start_equity: Decimal
    backtest_fee_pct: Decimal
    backtest_slippage_pct: Decimal
    backtest_funding_rate_8h: Decimal
    state_file: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv_if_available()
        symbols_raw = os.getenv("SYMBOLS", "")
        if not symbols_raw:
            symbols_raw = os.getenv("SYMBOL", "BTC/USDT")
        market_type = os.getenv("MARKET_TYPE", "swap").strip().lower()
        symbols = [normalize_symbol(s.strip().upper(), market_type) for s in symbols_raw.split(",") if s.strip()]
        entry_timeframe = os.getenv("ENTRY_TIMEFRAME", os.getenv("TIMEFRAME", "30m")).strip()
        return cls(
            api_key=os.getenv("OKX_API_KEY", "").strip(),
            secret_key=os.getenv("OKX_SECRET_KEY", "").strip(),
            passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
            okx_simulated_trading=env_bool("OKX_SIMULATED_TRADING", True),
            dry_run=env_bool("DRY_RUN", True),
            enable_live_trading=env_bool("ENABLE_LIVE_TRADING", False),
            symbols=symbols,
            timeframe=entry_timeframe,
            entry_timeframe=entry_timeframe,
            confirmation_timeframes=env_list("CONFIRMATION_TIMEFRAMES", "1h,4h"),
            candle_limit=env_int("CANDLE_LIMIT", 200),
            poll_seconds=env_int("POLL_SECONDS", 60),
            market_type=market_type,
            margin_mode=os.getenv("MARGIN_MODE", "isolated").strip().lower(),
            position_mode=os.getenv("POSITION_MODE", "auto").strip().lower(),
            risk_per_trade_pct=env_probability("RISK_PER_TRADE_PCT", "0.01"),
            daily_max_loss_pct=env_probability("DAILY_MAX_LOSS_PCT", "0.06"),
            max_consecutive_daily_losses=env_int("MAX_CONSECUTIVE_DAILY_LOSSES", 3),
            strategy=normalize_strategy(os.getenv("STRATEGY", "combined")),
            signal_confidence_threshold=env_probability("SIGNAL_CONFIDENCE_THRESHOLD", "0.68"),
            long_confidence_threshold=env_probability("LONG_CONFIDENCE_THRESHOLD", "0.72"),
            short_confidence_threshold=env_probability("SHORT_CONFIDENCE_THRESHOLD", "0.68"),
            symbol_confidence_thresholds=env_score_map("SYMBOL_CONFIDENCE_THRESHOLDS"),
            layered_smc_enabled=env_bool("LAYERED_SMC_ENABLED", False),
            layered_market_ma_period=env_int("LAYERED_MARKET_MA_PERIOD", 50),
            layered_market_slope_lookback=env_int("LAYERED_MARKET_SLOPE_LOOKBACK", 10),
            layered_market_min_trend_pct=env_probability("LAYERED_MARKET_MIN_TREND_PCT", "0.003"),
            layered_market_min_atr_pct=env_probability("LAYERED_MARKET_MIN_ATR_PCT", "0.004"),
            layered_market_max_range_pct=env_probability("LAYERED_MARKET_MAX_RANGE_PCT", "0.030"),
            layered_entry_smc_min_score=env_probability("LAYERED_ENTRY_SMC_MIN_SCORE", "0.50"),
            layered_entry_require_bos=env_bool("LAYERED_ENTRY_REQUIRE_BOS", True),
            layered_entry_require_pullback=env_bool("LAYERED_ENTRY_REQUIRE_PULLBACK", True),
            layered_entry_pullback_tolerance_pct=env_probability("LAYERED_ENTRY_PULLBACK_TOLERANCE_PCT", "0.003"),
            layered_entry_max_chase_pct=env_probability("LAYERED_ENTRY_MAX_CHASE_PCT", "0.006"),
            layered_entry_order_flow_min_score=env_probability("LAYERED_ENTRY_ORDER_FLOW_MIN_SCORE", "0.20"),
            layered_entry_position_min_score=env_probability("LAYERED_ENTRY_POSITION_MIN_SCORE", "0.50"),
            layered_require_liquidity_sweep=env_bool("LAYERED_REQUIRE_LIQUIDITY_SWEEP", False),
            combined_swing_lookback=env_int("COMBINED_SWING_LOOKBACK", 3),
            combined_structure_lookback=env_int("COMBINED_STRUCTURE_LOOKBACK", 40),
            combined_order_flow_lookback=env_int("COMBINED_ORDER_FLOW_LOOKBACK", 20),
            combined_avwap_lookback=env_int("COMBINED_AVWAP_LOOKBACK", 80),
            combined_volume_profile_lookback=env_int("COMBINED_VOLUME_PROFILE_LOOKBACK", 80),
            combined_volume_profile_bins=env_int("COMBINED_VOLUME_PROFILE_BINS", 24),
            combined_value_area_pct=env_probability("COMBINED_VALUE_AREA_PCT", "0.70"),
            combined_sweep_tolerance_pct=env_decimal("COMBINED_SWEEP_TOLERANCE_PCT", "0.001"),
            combined_min_displacement_pct=env_decimal("COMBINED_MIN_DISPLACEMENT_PCT", "0.002"),
            combined_min_score=env_probability("COMBINED_MIN_SCORE", "0.68"),
            combined_min_edge=env_probability("COMBINED_MIN_EDGE", "0.12"),
            fast_ema=env_int("FAST_EMA", 9),
            slow_ema=env_int("SLOW_EMA", 21),
            rsi_period=env_int("RSI_PERIOD", 14),
            rsi_buy_max=env_decimal("RSI_BUY_MAX", "65"),
            rsi_sell_min=env_decimal("RSI_SELL_MIN", "70"),
            smc_swing_lookback=env_int("SMC_SWING_LOOKBACK", 3),
            smc_zone_lookback=env_int("SMC_ZONE_LOOKBACK", 40),
            smc_zone_tolerance_pct=env_decimal("SMC_ZONE_TOLERANCE_PCT", "0.003"),
            smc_min_displacement_pct=env_decimal("SMC_MIN_DISPLACEMENT_PCT", "0.002"),
            smc_require_fvg=env_bool("SMC_REQUIRE_FVG", False),
            external_context_enabled=env_bool("EXTERNAL_CONTEXT_ENABLED", True),
            newsapi_enabled=env_bool("NEWSAPI_ENABLED", True),
            gdelt_enabled=env_bool("GDELT_ENABLED", True),
            fear_greed_enabled=env_bool("FEAR_GREED_ENABLED", True),
            fundamental_context_enabled=env_bool("FUNDAMENTAL_CONTEXT_ENABLED", True),
            newsapi_api_key=os.getenv("NEWSAPI_API_KEY", "").strip(),
            newsapi_page_size=env_int("NEWSAPI_PAGE_SIZE", 20),
            external_context_lookback_hours=env_int("EXTERNAL_CONTEXT_LOOKBACK_HOURS", 24),
            external_context_cache_seconds=env_int("EXTERNAL_CONTEXT_CACHE_SECONDS", 300),
            external_context_timeout_seconds=env_int("EXTERNAL_CONTEXT_TIMEOUT_SECONDS", 15),
            external_context_max_confidence_adjustment=env_probability("EXTERNAL_CONTEXT_MAX_CONFIDENCE_ADJUSTMENT", "0.15"),
            external_context_min_support=env_decimal("EXTERNAL_CONTEXT_MIN_SUPPORT", "-0.35"),
            external_context_extreme_threshold=env_probability("EXTERNAL_CONTEXT_EXTREME_THRESHOLD", "0.75"),
            external_context_risk_multiplier=env_probability("EXTERNAL_CONTEXT_RISK_MULTIPLIER", "0.50"),
            fear_greed_mode=os.getenv("FEAR_GREED_MODE", "momentum").strip().lower(),
            fundamental_bias=env_score_map("FUNDAMENTAL_BIAS"),
            add_position_enabled=env_bool("ADD_POSITION_ENABLED", True),
            max_position_adds=env_int("MAX_POSITION_ADDS", 2),
            add_position_quote_fraction=env_decimal("ADD_POSITION_QUOTE_FRACTION", "0.5"),
            add_position_require_profit=env_bool("ADD_POSITION_REQUIRE_PROFIT", True),
            add_position_min_profit_pct=env_decimal("ADD_POSITION_MIN_PROFIT_PCT", "0.005"),
            add_position_breakout_lookback=env_int("ADD_POSITION_BREAKOUT_LOOKBACK", 20),
            add_position_pullback_ma_period=env_int("ADD_POSITION_PULLBACK_MA_PERIOD", 20),
            add_position_support_lookback=env_int("ADD_POSITION_SUPPORT_LOOKBACK", 20),
            add_position_support_tolerance_pct=env_decimal("ADD_POSITION_SUPPORT_TOLERANCE_PCT", "0.003"),
            add_position_volume_multiplier=env_decimal("ADD_POSITION_VOLUME_MULTIPLIER", "1.2"),
            order_quote_amount=env_decimal("ORDER_QUOTE_AMOUNT", "10"),
            max_quote_per_order=env_decimal("MAX_QUOTE_PER_ORDER", "10"),
            stop_loss_pct=env_decimal("STOP_LOSS_PCT", "0.02"),
            symbol_stop_loss_pcts=env_score_map("SYMBOL_STOP_LOSS_PCTS"),
            take_profit_pct=env_decimal("TAKE_PROFIT_PCT", "0.04"),
            symbol_take_profit_pcts=env_score_map("SYMBOL_TAKE_PROFIT_PCTS"),
            attach_tp_sl=env_bool("ATTACH_TP_SL", True),
            sell_fraction=env_decimal("SELL_FRACTION", "1"),
            dynamic_exit_enabled=env_bool("DYNAMIC_EXIT_ENABLED", True),
            dynamic_exit_symbols=env_list("DYNAMIC_EXIT_SYMBOLS", "ETH,SOL"),
            dynamic_exit_atr_period=env_int("DYNAMIC_EXIT_ATR_PERIOD", 14),
            dynamic_exit_structure_lookback=env_int("DYNAMIC_EXIT_STRUCTURE_LOOKBACK", 20),
            dynamic_exit_min_stop_pct=env_probability("DYNAMIC_EXIT_MIN_STOP_PCT", "0.012"),
            dynamic_exit_max_stop_pct=env_probability("DYNAMIC_EXIT_MAX_STOP_PCT", "0.030"),
            dynamic_exit_atr_multiplier=env_decimal("DYNAMIC_EXIT_ATR_MULTIPLIER", "1.2"),
            dynamic_exit_base_rr=env_decimal("DYNAMIC_EXIT_BASE_RR", "2.0"),
            dynamic_exit_strong_rr=env_decimal("DYNAMIC_EXIT_STRONG_RR", "4.0"),
            dynamic_exit_strong_confidence=env_probability("DYNAMIC_EXIT_STRONG_CONFIDENCE", "0.70"),
            dynamic_exit_trend_ma_period=env_int("DYNAMIC_EXIT_TREND_MA_PERIOD", 20),
            exit_breakeven_r=env_decimal("EXIT_BREAKEVEN_R", "2.0"),
            exit_partial_take_profit_r=env_decimal("EXIT_PARTIAL_TAKE_PROFIT_R", "3.0"),
            exit_partial_fraction=env_probability("EXIT_PARTIAL_FRACTION", "0.5"),
            exit_trailing_atr_multiplier=env_decimal("EXIT_TRAILING_ATR_MULTIPLIER", "1.5"),
            exit_trailing_enabled=env_bool("EXIT_TRAILING_ENABLED", True),
            backtest_start_equity=env_decimal("BACKTEST_START_EQUITY", "1000"),
            backtest_fee_pct=env_probability("BACKTEST_FEE_PCT", "0.0005"),
            backtest_slippage_pct=env_probability("BACKTEST_SLIPPAGE_PCT", "0.0005"),
            backtest_funding_rate_8h=env_probability("BACKTEST_FUNDING_RATE_8H", "0.0001"),
            state_file=os.getenv("STATE_FILE", "state.json").strip(),
        )

    @property
    def has_private_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    @property
    def analysis_timeframes(self) -> list[str]:
        timeframes: list[str] = []
        for timeframe in [self.entry_timeframe, *self.confirmation_timeframes]:
            if timeframe and timeframe not in timeframes:
                timeframes.append(timeframe)
        return timeframes

    def confidence_threshold_for_symbol(self, symbol: str) -> Decimal:
        for candidate in self.symbol_threshold_candidates(symbol):
            if candidate in self.symbol_confidence_thresholds:
                return self.symbol_confidence_thresholds[candidate]
        return self.signal_confidence_threshold

    def confidence_threshold_for_symbol_and_action(self, symbol: str, action: str) -> Decimal:
        threshold = self.confidence_threshold_for_symbol(symbol)
        if action == "buy":
            return max(threshold, self.long_confidence_threshold)
        if action == "sell":
            return max(threshold, self.short_confidence_threshold)
        return threshold

    def stop_loss_pct_for_symbol(self, symbol: str) -> Decimal:
        for candidate in self.symbol_threshold_candidates(symbol):
            if candidate in self.symbol_stop_loss_pcts:
                return self.symbol_stop_loss_pcts[candidate]
        return self.stop_loss_pct

    def take_profit_pct_for_symbol(self, symbol: str) -> Decimal:
        for candidate in self.symbol_threshold_candidates(symbol):
            if candidate in self.symbol_take_profit_pcts:
                return self.symbol_take_profit_pcts[candidate]
        return self.take_profit_pct

    @staticmethod
    def symbol_threshold_candidates(symbol: str) -> list[str]:
        upper_symbol = symbol.strip().upper()
        without_settle = upper_symbol.split(":", 1)[0]
        base = without_settle.split("/", 1)[0]
        candidates = [upper_symbol, without_settle, base]
        return [candidate for index, candidate in enumerate(candidates) if candidate and candidate not in candidates[:index]]

    def validate(self, require_private: bool = False, require_order_submission: bool = True) -> None:
        if self.market_type != "swap":
            raise ConfigError("This bot is swap-only. Set MARKET_TYPE=swap.")
        if self.margin_mode not in {"isolated", "cross"}:
            raise ConfigError("MARGIN_MODE must be isolated or cross.")
        if self.position_mode not in {"auto", "net", "hedge"}:
            raise ConfigError("POSITION_MODE must be auto, net, or hedge.")
        if not Decimal("0") < self.risk_per_trade_pct <= Decimal("1"):
            raise ConfigError("RISK_PER_TRADE_PCT must be between 0 and 1, or 0 and 100 percent.")
        if not Decimal("0") < self.daily_max_loss_pct <= Decimal("1"):
            raise ConfigError("DAILY_MAX_LOSS_PCT must be between 0 and 1, or 0 and 100 percent.")
        if self.max_consecutive_daily_losses < 0:
            raise ConfigError("MAX_CONSECUTIVE_DAILY_LOSSES cannot be negative. Use 0 to disable this guard.")
        if self.strategy != "combined":
            raise ConfigError("STRATEGY must be combined. Legacy ema_rsi/smc values are mapped to combined automatically.")
        if not self.entry_timeframe:
            raise ConfigError("ENTRY_TIMEFRAME is required.")
        if not self.confirmation_timeframes:
            raise ConfigError("CONFIRMATION_TIMEFRAMES must include at least one higher timeframe.")
        if not Decimal("0") <= self.signal_confidence_threshold <= Decimal("1"):
            raise ConfigError("SIGNAL_CONFIDENCE_THRESHOLD must be between 0 and 1, or 0 and 100 percent.")
        if not Decimal("0") <= self.long_confidence_threshold <= Decimal("1"):
            raise ConfigError("LONG_CONFIDENCE_THRESHOLD must be between 0 and 1, or 0 and 100 percent.")
        if not Decimal("0") <= self.short_confidence_threshold <= Decimal("1"):
            raise ConfigError("SHORT_CONFIDENCE_THRESHOLD must be between 0 and 1, or 0 and 100 percent.")
        for symbol, threshold in self.symbol_confidence_thresholds.items():
            if not Decimal("0") <= threshold <= Decimal("1"):
                raise ConfigError(f"SYMBOL_CONFIDENCE_THRESHOLDS for {symbol} must be between 0 and 1, or 0 and 100 percent.")
        if self.layered_market_ma_period < 2:
            raise ConfigError("LAYERED_MARKET_MA_PERIOD must be at least 2.")
        if self.layered_market_slope_lookback < 1:
            raise ConfigError("LAYERED_MARKET_SLOPE_LOOKBACK must be at least 1.")
        if self.layered_market_min_trend_pct < 0 or self.layered_market_min_atr_pct < 0 or self.layered_market_max_range_pct < 0:
            raise ConfigError("Layered SMC market percentage settings cannot be negative.")
        if not Decimal("0") <= self.layered_entry_smc_min_score <= Decimal("1"):
            raise ConfigError("LAYERED_ENTRY_SMC_MIN_SCORE must be between 0 and 1.")
        if self.layered_entry_pullback_tolerance_pct < 0 or self.layered_entry_max_chase_pct < 0:
            raise ConfigError("Layered SMC pullback percentage settings cannot be negative.")
        if not Decimal("0") <= self.layered_entry_order_flow_min_score <= Decimal("1"):
            raise ConfigError("LAYERED_ENTRY_ORDER_FLOW_MIN_SCORE must be between 0 and 1.")
        if not Decimal("0") <= self.layered_entry_position_min_score <= Decimal("1"):
            raise ConfigError("LAYERED_ENTRY_POSITION_MIN_SCORE must be between 0 and 1.")
        if self.combined_swing_lookback < 2:
            raise ConfigError("COMBINED_SWING_LOOKBACK must be at least 2.")
        if self.combined_structure_lookback < 5:
            raise ConfigError("COMBINED_STRUCTURE_LOOKBACK must be at least 5.")
        if self.combined_order_flow_lookback < 2:
            raise ConfigError("COMBINED_ORDER_FLOW_LOOKBACK must be at least 2.")
        if self.combined_avwap_lookback < 5:
            raise ConfigError("COMBINED_AVWAP_LOOKBACK must be at least 5.")
        if self.combined_volume_profile_lookback < 5:
            raise ConfigError("COMBINED_VOLUME_PROFILE_LOOKBACK must be at least 5.")
        if self.combined_volume_profile_bins < 5:
            raise ConfigError("COMBINED_VOLUME_PROFILE_BINS must be at least 5.")
        if not Decimal("0") < self.combined_value_area_pct <= Decimal("1"):
            raise ConfigError("COMBINED_VALUE_AREA_PCT must be between 0 and 1, or 0 and 100 percent.")
        if self.combined_sweep_tolerance_pct < 0 or self.combined_min_displacement_pct < 0:
            raise ConfigError("Combined strategy percentage settings cannot be negative.")
        if not Decimal("0") <= self.combined_min_score <= Decimal("1"):
            raise ConfigError("COMBINED_MIN_SCORE must be between 0 and 1, or 0 and 100 percent.")
        if not Decimal("0") <= self.combined_min_edge <= Decimal("1"):
            raise ConfigError("COMBINED_MIN_EDGE must be between 0 and 1, or 0 and 100 percent.")
        minimum_combined_candles = max(
            self.combined_structure_lookback,
            self.combined_volume_profile_lookback,
            self.combined_avwap_lookback,
            self.combined_order_flow_lookback,
            (self.combined_swing_lookback * 2) + 5,
            self.layered_market_ma_period + self.layered_market_slope_lookback + 2,
            self.dynamic_exit_atr_period + 2,
        ) + 2
        if self.candle_limit < minimum_combined_candles:
            raise ConfigError(f"CANDLE_LIMIT must be at least {minimum_combined_candles} for the combined strategy.")
        if self.newsapi_page_size < 1 or self.newsapi_page_size > 100:
            raise ConfigError("NEWSAPI_PAGE_SIZE must be between 1 and 100.")
        if self.external_context_lookback_hours < 1:
            raise ConfigError("EXTERNAL_CONTEXT_LOOKBACK_HOURS must be at least 1.")
        if self.external_context_cache_seconds < 0:
            raise ConfigError("EXTERNAL_CONTEXT_CACHE_SECONDS cannot be negative.")
        if self.external_context_timeout_seconds < 1:
            raise ConfigError("EXTERNAL_CONTEXT_TIMEOUT_SECONDS must be at least 1.")
        if not Decimal("0") <= self.external_context_max_confidence_adjustment <= Decimal("1"):
            raise ConfigError("EXTERNAL_CONTEXT_MAX_CONFIDENCE_ADJUSTMENT must be between 0 and 1.")
        if not Decimal("-1") <= self.external_context_min_support <= Decimal("1"):
            raise ConfigError("EXTERNAL_CONTEXT_MIN_SUPPORT must be between -1 and 1.")
        if not Decimal("0") <= self.external_context_extreme_threshold <= Decimal("1"):
            raise ConfigError("EXTERNAL_CONTEXT_EXTREME_THRESHOLD must be between 0 and 1.")
        if not Decimal("0") < self.external_context_risk_multiplier <= Decimal("1"):
            raise ConfigError("EXTERNAL_CONTEXT_RISK_MULTIPLIER must be greater than 0 and no more than 1.")
        if self.fear_greed_mode not in {"momentum", "contrarian"}:
            raise ConfigError("FEAR_GREED_MODE must be momentum or contrarian.")
        for symbol, score in self.fundamental_bias.items():
            if not Decimal("-1") <= score <= Decimal("1"):
                raise ConfigError(f"FUNDAMENTAL_BIAS for {symbol} must be between -1 and 1, or -100 and 100 percent.")
        if self.max_position_adds < 0:
            raise ConfigError("MAX_POSITION_ADDS cannot be negative.")
        if not Decimal("0") < self.add_position_quote_fraction <= Decimal("1"):
            raise ConfigError("ADD_POSITION_QUOTE_FRACTION must be between 0 and 1.")
        if self.add_position_min_profit_pct < 0:
            raise ConfigError("ADD_POSITION_MIN_PROFIT_PCT cannot be negative.")
        if self.add_position_breakout_lookback < 2:
            raise ConfigError("ADD_POSITION_BREAKOUT_LOOKBACK must be at least 2.")
        if self.add_position_pullback_ma_period < 2:
            raise ConfigError("ADD_POSITION_PULLBACK_MA_PERIOD must be at least 2.")
        if self.add_position_support_lookback < 2:
            raise ConfigError("ADD_POSITION_SUPPORT_LOOKBACK must be at least 2.")
        if self.add_position_support_tolerance_pct < 0:
            raise ConfigError("ADD_POSITION_SUPPORT_TOLERANCE_PCT cannot be negative.")
        if self.add_position_volume_multiplier <= 0:
            raise ConfigError("ADD_POSITION_VOLUME_MULTIPLIER must be greater than 0.")
        if not self.symbols:
            raise ConfigError("SYMBOLS is required and must include at least one market symbol like BTC/USDT.")
        if any("/" not in symbol for symbol in self.symbols):
            raise ConfigError("SYMBOLS must be a comma-separated list of market symbols like BTC/USDT.")
        if any(symbol.split("/")[1].split(":")[0] != "USDT" for symbol in self.symbols):
            raise ConfigError("All SYMBOLS must use USDT as the quote currency.")
        if self.order_quote_amount <= 0:
            raise ConfigError("ORDER_QUOTE_AMOUNT must be greater than 0.")
        if self.max_quote_per_order <= 0:
            raise ConfigError("MAX_QUOTE_PER_ORDER must be greater than 0.")
        if self.order_quote_amount > self.max_quote_per_order:
            raise ConfigError("ORDER_QUOTE_AMOUNT cannot exceed MAX_QUOTE_PER_ORDER.")
        if not Decimal("0") < self.sell_fraction <= Decimal("1"):
            raise ConfigError("SELL_FRACTION must be between 0 and 1.")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ConfigError("STOP_LOSS_PCT and TAKE_PROFIT_PCT must be greater than 0.")
        for symbol, stop_loss_pct in self.symbol_stop_loss_pcts.items():
            if stop_loss_pct <= 0:
                raise ConfigError(f"SYMBOL_STOP_LOSS_PCTS for {symbol} must be greater than 0.")
        for symbol, take_profit_pct in self.symbol_take_profit_pcts.items():
            if take_profit_pct <= 0:
                raise ConfigError(f"SYMBOL_TAKE_PROFIT_PCTS for {symbol} must be greater than 0.")
        if self.backtest_fee_pct < 0 or self.backtest_slippage_pct < 0 or self.backtest_funding_rate_8h < 0:
            raise ConfigError("Backtest fee, slippage, and funding settings cannot be negative.")
        if self.dynamic_exit_atr_period < 2:
            raise ConfigError("DYNAMIC_EXIT_ATR_PERIOD must be at least 2.")
        if self.dynamic_exit_structure_lookback < 2:
            raise ConfigError("DYNAMIC_EXIT_STRUCTURE_LOOKBACK must be at least 2.")
        if self.dynamic_exit_trend_ma_period < 2:
            raise ConfigError("DYNAMIC_EXIT_TREND_MA_PERIOD must be at least 2.")
        if not Decimal("0") < self.dynamic_exit_min_stop_pct <= self.dynamic_exit_max_stop_pct:
            raise ConfigError("DYNAMIC_EXIT_MIN_STOP_PCT must be greater than 0 and no larger than DYNAMIC_EXIT_MAX_STOP_PCT.")
        if self.dynamic_exit_atr_multiplier <= 0 or self.dynamic_exit_base_rr <= 0 or self.dynamic_exit_strong_rr <= 0:
            raise ConfigError("Dynamic exit multipliers and RR settings must be greater than 0.")
        if self.dynamic_exit_strong_rr < self.dynamic_exit_base_rr:
            raise ConfigError("DYNAMIC_EXIT_STRONG_RR must be greater than or equal to DYNAMIC_EXIT_BASE_RR.")
        if not Decimal("0") <= self.dynamic_exit_strong_confidence <= Decimal("1"):
            raise ConfigError("DYNAMIC_EXIT_STRONG_CONFIDENCE must be between 0 and 1, or 0 and 100 percent.")
        if self.exit_breakeven_r <= 0 or self.exit_partial_take_profit_r <= 0 or self.exit_trailing_atr_multiplier <= 0:
            raise ConfigError("EXIT_BREAKEVEN_R, EXIT_PARTIAL_TAKE_PROFIT_R, and EXIT_TRAILING_ATR_MULTIPLIER must be greater than 0.")
        if self.exit_partial_take_profit_r < self.exit_breakeven_r:
            raise ConfigError("EXIT_PARTIAL_TAKE_PROFIT_R must be greater than or equal to EXIT_BREAKEVEN_R.")
        if not Decimal("0") < self.exit_partial_fraction < Decimal("1"):
            raise ConfigError("EXIT_PARTIAL_FRACTION must be greater than 0 and less than 1.")
        if self.backtest_start_equity <= 0:
            raise ConfigError("BACKTEST_START_EQUITY must be greater than 0.")
        if require_private and not self.has_private_credentials:
            raise ConfigError("Private credentials are required for this command.")
        if require_order_submission:
            if not self.dry_run and not self.has_private_credentials:
                raise ConfigError("Set OKX_API_KEY, OKX_SECRET_KEY, and OKX_PASSPHRASE before submitting orders.")
            if not self.dry_run and not self.okx_simulated_trading and not self.enable_live_trading:
                raise ConfigError(
                    "Live trading is blocked. Set ENABLE_LIVE_TRADING=true only after testing dry-run and demo mode."
                )
