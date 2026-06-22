from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import BotConfig


POSITIVE_TERMS = {
    "adoption",
    "approval",
    "approved",
    "bullish",
    "breakthrough",
    "growth",
    "inflow",
    "institutional",
    "partnership",
    "rally",
    "record",
    "surge",
    "upgrade",
}

NEGATIVE_TERMS = {
    "ban",
    "bearish",
    "crackdown",
    "crash",
    "exploit",
    "fraud",
    "hack",
    "lawsuit",
    "liquidation",
    "outflow",
    "plunge",
    "probe",
    "scam",
    "sec",
}

ASSET_TERMS = {
    "BTC": "bitcoin OR btc",
    "ETH": "ethereum OR ether OR eth",
    "SOL": "solana OR sol",
    "XRP": "xrp OR ripple",
    "DOGE": "dogecoin OR doge",
}


@dataclass(frozen=True)
class ContextSnapshot:
    combined_score: Decimal = Decimal("0")
    newsapi_score: Decimal | None = None
    gdelt_score: Decimal | None = None
    fear_greed_score: Decimal | None = None
    fundamental_score: Decimal | None = None
    sources_used: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


class ExternalContextService:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.cache: dict[str, tuple[float, ContextSnapshot]] = {}

    def evaluate(self, symbol: str) -> ContextSnapshot:
        cache_key = self.base_asset(symbol)
        cached = self.cache.get(cache_key)
        now = time.time()
        if cached and (now - cached[0]) <= self.config.external_context_cache_seconds:
            return cached[1]

        snapshot = self.fetch_snapshot(symbol)
        self.cache[cache_key] = (now, snapshot)
        return snapshot

    def fetch_snapshot(self, symbol: str) -> ContextSnapshot:
        scores: list[Decimal] = []
        details: dict[str, Any] = {}
        errors: list[str] = []
        newsapi_score: Decimal | None = None
        gdelt_score: Decimal | None = None
        fear_greed_score: Decimal | None = None
        fundamental_score: Decimal | None = None

        if self.config.fundamental_context_enabled:
            fundamental_score = self.fetch_fundamental_bias(symbol)
            if fundamental_score is not None:
                scores.append(fundamental_score)
                details["fundamental_bias"] = float(fundamental_score)

        if self.config.newsapi_enabled and self.config.newsapi_api_key:
            try:
                newsapi_score, count = self.fetch_newsapi_score(symbol)
                scores.append(newsapi_score)
                details["newsapi_articles"] = count
            except Exception as exc:
                logging.warning("NewsAPI context failed for %s: %s", symbol, exc)
                errors.append(f"newsapi:{exc}")
        elif self.config.newsapi_enabled:
            details["newsapi"] = "skipped_missing_api_key"

        if self.config.gdelt_enabled:
            try:
                gdelt_score, points = self.fetch_gdelt_score(symbol)
                scores.append(gdelt_score)
                details["gdelt_points"] = points
            except Exception as exc:
                logging.warning("GDELT context failed for %s: %s", symbol, exc)
                errors.append(f"gdelt:{exc}")

        if self.config.fear_greed_enabled:
            try:
                fear_greed_score, value, classification = self.fetch_fear_greed_score()
                scores.append(fear_greed_score)
                details["fear_greed_value"] = value
                details["fear_greed_classification"] = classification
            except Exception as exc:
                logging.warning("Fear & Greed context failed: %s", exc)
                errors.append(f"fear_greed:{exc}")

        combined = self.average(scores)
        return ContextSnapshot(
            combined_score=combined,
            newsapi_score=newsapi_score,
            gdelt_score=gdelt_score,
            fear_greed_score=fear_greed_score,
            fundamental_score=fundamental_score,
            sources_used=len(scores),
            details=details,
            errors=tuple(errors),
        )

    def fetch_newsapi_score(self, symbol: str) -> tuple[Decimal, int]:
        query = self.news_query(symbol)
        from_time = datetime.now(timezone.utc) - timedelta(hours=self.config.external_context_lookback_hours)
        params = {
            "q": query,
            "searchIn": "title,description",
            "language": "en",
            "sortBy": "publishedAt",
            "from": from_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "pageSize": str(self.config.newsapi_page_size),
        }
        url = "https://newsapi.org/v2/everything?" + urlencode(params)
        data = self.http_get_json(url, headers={"X-Api-Key": self.config.newsapi_api_key})
        articles = data.get("articles", [])
        if not isinstance(articles, list):
            return Decimal("0"), 0

        scores = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            text = " ".join(
                str(article.get(field) or "")
                for field in ("title", "description", "content")
            )
            scores.append(self.keyword_sentiment(text))
        return self.average(scores), len(scores)

    def fetch_gdelt_score(self, symbol: str) -> tuple[Decimal, int]:
        params = {
            "query": self.news_query(symbol),
            "mode": "timelinetone",
            "format": "json",
            "timespan": f"{self.config.external_context_lookback_hours}h",
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urlencode(params)
        data = self.http_get_json(url)
        tones: list[Decimal] = []
        self.collect_tone_values(data, tones)
        if not tones:
            return Decimal("0"), 0
        normalized = [self.clamp(tone / Decimal("10")) for tone in tones[-20:]]
        return self.average(normalized), len(tones)

    def fetch_fear_greed_score(self) -> tuple[Decimal, int, str]:
        data = self.http_get_json("https://api.alternative.me/fng/?limit=1")
        items = data.get("data", [])
        if not isinstance(items, list) or not items:
            return Decimal("0"), 50, "unknown"
        item = items[0]
        if not isinstance(item, dict):
            return Decimal("0"), 50, "unknown"
        value = int(item.get("value") or 50)
        classification = str(item.get("value_classification") or "unknown")
        score = self.clamp((Decimal(value) - Decimal("50")) / Decimal("50"))
        if self.config.fear_greed_mode == "contrarian":
            score = -score
        return score, value, classification

    def fetch_fundamental_bias(self, symbol: str) -> Decimal | None:
        base = self.base_asset(symbol)
        return self.config.fundamental_bias.get(base)

    def http_get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = Request(url, headers=headers or {})
        with urlopen(request, timeout=self.config.external_context_timeout_seconds) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def news_query(self, symbol: str) -> str:
        base = self.base_asset(symbol)
        asset_terms = ASSET_TERMS.get(base, base.lower())
        return f"({asset_terms}) AND (crypto OR cryptocurrency OR blockchain OR market OR regulation OR ETF)"

    @staticmethod
    def base_asset(symbol: str) -> str:
        return symbol.split("/")[0].upper()

    @classmethod
    def keyword_sentiment(cls, text: str) -> Decimal:
        words = set(text.lower().replace("-", " ").split())
        positive = sum(1 for word in POSITIVE_TERMS if word in words)
        negative = sum(1 for word in NEGATIVE_TERMS if word in words)
        total = positive + negative
        if total == 0:
            return Decimal("0")
        return cls.clamp(Decimal(positive - negative) / Decimal(total))

    @classmethod
    def collect_tone_values(cls, value: Any, tones: list[Decimal]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).lower()
                if "tone" in normalized_key and cls.is_number(child):
                    tones.append(Decimal(str(child)))
                else:
                    cls.collect_tone_values(child, tones)
        elif isinstance(value, list):
            for child in value:
                cls.collect_tone_values(child, tones)

    @staticmethod
    def is_number(value: Any) -> bool:
        try:
            Decimal(str(value))
        except Exception:
            return False
        return True

    @classmethod
    def average(cls, values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        return cls.clamp(sum(values, Decimal("0")) / Decimal(len(values)))

    @staticmethod
    def clamp(value: Decimal) -> Decimal:
        return max(Decimal("-1"), min(Decimal("1"), value))
