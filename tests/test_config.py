import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig, ConfigError


class ConfigTests(unittest.TestCase):
    def test_live_trading_requires_explicit_guard(self) -> None:
        env = {
            "DRY_RUN": "false",
            "OKX_SIMULATED_TRADING": "false",
            "ENABLE_LIVE_TRADING": "false",
            "OKX_API_KEY": "key",
            "OKX_SECRET_KEY": "secret",
            "OKX_PASSPHRASE": "passphrase",
        }
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            with self.assertRaises(ConfigError):
                config.validate()

    def test_default_config_is_dry_run(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {}, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertTrue(config.dry_run)
            self.assertTrue(config.okx_simulated_trading)

    def test_unknown_strategy_is_rejected(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {"STRATEGY": "unknown"}, clear=True):
            config = BotConfig.from_env()
            with self.assertRaises(ConfigError):
                config.validate()

    def test_spot_market_type_is_rejected(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {"MARKET_TYPE": "spot"}, clear=True):
            config = BotConfig.from_env()
            with self.assertRaises(ConfigError):
                config.validate()

    def test_signal_confidence_threshold_accepts_percent(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {"SIGNAL_CONFIDENCE_THRESHOLD": "80"}, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertEqual(str(config.signal_confidence_threshold), "0.8")

    def test_symbol_confidence_threshold_resolves_base_symbol(self) -> None:
        env = {
            "SYMBOL_CONFIDENCE_THRESHOLDS": "BTC:0.72,ETH:0.68",
        }
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertEqual(config.confidence_threshold_for_symbol("BTC/USDT:USDT"), config.symbol_confidence_thresholds["BTC"])
            self.assertEqual(str(config.confidence_threshold_for_symbol("ETH/USDT:USDT")), "0.68")
            self.assertEqual(str(config.confidence_threshold_for_symbol("SOL/USDT:USDT")), "0.68")

    def test_backtest_cost_settings_are_validated(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {"BACKTEST_FEE_PCT": "-0.1"}, clear=True):
            config = BotConfig.from_env()
            with self.assertRaises(ConfigError):
                config.validate()

    def test_symbol_exit_settings_resolve_base_symbol(self) -> None:
        env = {
            "SYMBOL_STOP_LOSS_PCTS": "ETH:0.015",
            "SYMBOL_TAKE_PROFIT_PCTS": "ETH:0.06",
        }
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertEqual(str(config.stop_loss_pct_for_symbol("ETH/USDT:USDT")), "0.015")
            self.assertEqual(str(config.take_profit_pct_for_symbol("ETH/USDT:USDT")), "0.06")
            self.assertEqual(str(config.stop_loss_pct_for_symbol("BTC/USDT:USDT")), "0.02")
            self.assertEqual(str(config.take_profit_pct_for_symbol("BTC/USDT:USDT")), "0.04")

    def test_external_context_cache_defaults_to_five_minutes(self) -> None:
        with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, {}, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertEqual(config.external_context_cache_seconds, 300)


if __name__ == "__main__":
    unittest.main()
