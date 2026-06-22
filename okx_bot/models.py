from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    price: Decimal
    indicators: dict[str, float] = field(default_factory=dict)
    confidence: Decimal = Decimal("0")
