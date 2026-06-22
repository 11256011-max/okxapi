from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


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
    candle_limit: int
    poll_seconds: int
    market_type: str
    leverage: Decimal
    strategy: str
    signal_confidence_threshold: Decimal
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
    order_quote_amount: Decimal
    max_quote_per_order: Decimal
    max_daily_notional: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    attach_tp_sl: bool
    sell_fraction: Decimal
    state_file: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv_if_available()
        symbols_raw = os.getenv("SYMBOLS", "")
        if not symbols_raw:
            symbols_raw = os.getenv("SYMBOL", "BTC/USDT")
        symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
        return cls(
            api_key=os.getenv("OKX_API_KEY", "").strip(),
            secret_key=os.getenv("OKX_SECRET_KEY", "").strip(),
            passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
            okx_simulated_trading=env_bool("OKX_SIMULATED_TRADING", True),
            dry_run=env_bool("DRY_RUN", True),
            enable_live_trading=env_bool("ENABLE_LIVE_TRADING", False),
            symbols=symbols,
            timeframe=os.getenv("TIMEFRAME", "1m").strip(),
            candle_limit=env_int("CANDLE_LIMIT", 200),
            poll_seconds=env_int("POLL_SECONDS", 60),
            market_type=os.getenv("MARKET_TYPE", "spot").strip().lower(),
            leverage=env_decimal("LEVERAGE", "1"),
            strategy=os.getenv("STRATEGY", "ema_rsi").strip().lower(),
            signal_confidence_threshold=env_probability("SIGNAL_CONFIDENCE_THRESHOLD", "0.90"),
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
            order_quote_amount=env_decimal("ORDER_QUOTE_AMOUNT", "10"),
            max_quote_per_order=env_decimal("MAX_QUOTE_PER_ORDER", "10"),
            max_daily_notional=env_decimal("MAX_DAILY_NOTIONAL", "50"),
            stop_loss_pct=env_decimal("STOP_LOSS_PCT", "0.02"),
            take_profit_pct=env_decimal("TAKE_PROFIT_PCT", "0.04"),
            attach_tp_sl=env_bool("ATTACH_TP_SL", True),
            sell_fraction=env_decimal("SELL_FRACTION", "1"),
            state_file=os.getenv("STATE_FILE", "state.json").strip(),
        )

    @property
    def has_private_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    def validate(self, require_private: bool = False) -> None:
        if self.market_type != "spot":
            raise ConfigError("This starter bot is spot-only. Keep MARKET_TYPE=spot.")
        if self.leverage != Decimal("1"):
            raise ConfigError("This starter bot does not use leverage. Keep LEVERAGE=1.")
        if self.strategy not in {"ema_rsi", "smc"}:
            raise ConfigError("STRATEGY must be one of: ema_rsi, smc.")
        if not Decimal("0") <= self.signal_confidence_threshold <= Decimal("1"):
            raise ConfigError("SIGNAL_CONFIDENCE_THRESHOLD must be between 0 and 1, or 0 and 100 percent.")
        if self.strategy == "ema_rsi":
            if self.fast_ema <= 1 or self.slow_ema <= 1:
                raise ConfigError("FAST_EMA and SLOW_EMA must be greater than 1.")
            if self.fast_ema >= self.slow_ema:
                raise ConfigError("FAST_EMA must be smaller than SLOW_EMA.")
            if self.candle_limit < max(self.slow_ema, self.rsi_period) + 5:
                raise ConfigError("CANDLE_LIMIT is too small for the configured indicators.")
        if self.strategy == "smc":
            if self.smc_swing_lookback < 2:
                raise ConfigError("SMC_SWING_LOOKBACK must be at least 2.")
            if self.smc_zone_lookback < 5:
                raise ConfigError("SMC_ZONE_LOOKBACK must be at least 5.")
            if self.smc_zone_tolerance_pct < 0 or self.smc_min_displacement_pct < 0:
                raise ConfigError("SMC percentage settings cannot be negative.")
            minimum_smc_candles = (self.smc_swing_lookback * 2) + self.smc_zone_lookback + 5
            if self.candle_limit < minimum_smc_candles:
                raise ConfigError(f"CANDLE_LIMIT must be at least {minimum_smc_candles} for SMC.")
        if not self.symbols:
            raise ConfigError("SYMBOLS is required and must include at least one market symbol like BTC/USDT.")
        if any("/" not in symbol for symbol in self.symbols):
            raise ConfigError("SYMBOLS must be a comma-separated list of market symbols like BTC/USDT.")
        if any(symbol.split("/")[1] != "USDT" for symbol in self.symbols):
            raise ConfigError("All SYMBOLS must use USDT as the quote currency.")
        if self.order_quote_amount <= 0:
            raise ConfigError("ORDER_QUOTE_AMOUNT must be greater than 0.")
        if self.max_quote_per_order <= 0 or self.max_daily_notional <= 0:
            raise ConfigError("MAX_QUOTE_PER_ORDER and MAX_DAILY_NOTIONAL must be greater than 0.")
        if self.order_quote_amount > self.max_quote_per_order:
            raise ConfigError("ORDER_QUOTE_AMOUNT cannot exceed MAX_QUOTE_PER_ORDER.")
        if not Decimal("0") < self.sell_fraction <= Decimal("1"):
            raise ConfigError("SELL_FRACTION must be between 0 and 1.")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ConfigError("STOP_LOSS_PCT and TAKE_PROFIT_PCT must be greater than 0.")
        if require_private and not self.has_private_credentials:
            raise ConfigError("Private credentials are required for this command.")
        if not self.dry_run and not self.has_private_credentials:
            raise ConfigError("Set OKX_API_KEY, OKX_SECRET_KEY, and OKX_PASSPHRASE before submitting orders.")
        if not self.dry_run and not self.okx_simulated_trading and not self.enable_live_trading:
            raise ConfigError(
                "Live trading is blocked. Set ENABLE_LIVE_TRADING=true only after testing dry-run and demo mode."
            )
