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


if __name__ == "__main__":
    unittest.main()
