from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from okx_bot.config import BotConfig
from okx_bot.external_context import ContextSnapshot, ExternalContextService


def make_config(extra_env: dict[str, str] | None = None) -> BotConfig:
    env = {
        "EXTERNAL_CONTEXT_ENABLED": "true",
        "NEWSAPI_ENABLED": "false",
        "GDELT_ENABLED": "false",
        "FEAR_GREED_ENABLED": "false",
        "FUNDAMENTAL_CONTEXT_ENABLED": "true",
        "FUNDAMENTAL_BIAS": "BTC:0.25,SOL:-0.10",
    }
    env.update(extra_env or {})
    with patch("okx_bot.config.load_dotenv_if_available"), patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()
        config.validate()
        return config


class CountingExternalContextService(ExternalContextService):
    def __init__(self, config: BotConfig) -> None:
        super().__init__(config)
        self.fetches = 0

    def fetch_snapshot(self, symbol: str) -> ContextSnapshot:
        self.fetches += 1
        return ContextSnapshot(combined_score=Decimal("0.25"), sources_used=1)


class ExternalContextTests(unittest.TestCase):
    def test_fundamental_bias_supports_per_symbol_scores(self) -> None:
        service = ExternalContextService(make_config())

        btc = service.fetch_snapshot("BTC/USDT:USDT")
        sol = service.fetch_snapshot("SOL/USDT:USDT")

        self.assertEqual(btc.combined_score, Decimal("0.25"))
        self.assertEqual(sol.combined_score, Decimal("-0.10"))

    def test_cache_reuses_snapshot_for_five_minutes(self) -> None:
        service = CountingExternalContextService(make_config({"EXTERNAL_CONTEXT_CACHE_SECONDS": "300"}))

        first = service.evaluate("BTC/USDT:USDT")
        second = service.evaluate("BTC/USDT:USDT")

        self.assertIs(first, second)
        self.assertEqual(service.fetches, 1)

    def test_fear_greed_momentum_scores_greed_positive(self) -> None:
        service = ExternalContextService(make_config())
        service.http_get_json = lambda url, headers=None: {
            "data": [{"value": "75", "value_classification": "Greed"}]
        }

        score, value, classification = service.fetch_fear_greed_score()

        self.assertEqual(score, Decimal("0.5"))
        self.assertEqual(value, 75)
        self.assertEqual(classification, "Greed")


if __name__ == "__main__":
    unittest.main()
