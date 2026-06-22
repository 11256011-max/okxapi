from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import BotConfig
from .state import BotState


class RiskError(RuntimeError):
    """Raised when a trade violates a configured risk rule."""


@dataclass(frozen=True)
class RiskDecision:
    quote_amount: Decimal


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def approve_buy(self, state: BotState) -> RiskDecision:
        quote_amount = min(self.config.order_quote_amount, self.config.max_quote_per_order)
        projected_daily_notional = state.daily_notional + quote_amount
        if projected_daily_notional > self.config.max_daily_notional:
            raise RiskError(
                f"Daily notional limit reached: {projected_daily_notional} > {self.config.max_daily_notional}"
            )
        return RiskDecision(quote_amount=quote_amount)

    def stop_loss_hit(self, state: BotState, current_price: Decimal) -> bool:
        if state.position_base <= 0 or state.entry_price <= 0:
            return False
        stop_price = state.entry_price * (Decimal("1") - self.config.stop_loss_pct)
        return current_price <= stop_price

    def take_profit_hit(self, state: BotState, current_price: Decimal) -> bool:
        if state.position_base <= 0 or state.entry_price <= 0:
            return False
        target_price = state.entry_price * (Decimal("1") + self.config.take_profit_pct)
        return current_price >= target_price

