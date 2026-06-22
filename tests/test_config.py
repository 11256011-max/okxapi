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
        with patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            with self.assertRaises(ConfigError):
                config.validate()

    def test_default_config_is_dry_run(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = BotConfig.from_env()
            config.validate()
            self.assertTrue(config.dry_run)
            self.assertTrue(config.okx_simulated_trading)


if __name__ == "__main__":
    unittest.main()

