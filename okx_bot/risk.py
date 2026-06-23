from __future__ import annotations

from decimal import Decimal

from .config import BotConfig
from .state import BotState


class RiskError(RuntimeError):
    """Raised when a trade violates a configured risk rule."""

class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def stop_loss_hit(self, state: BotState, symbol: str, current_price: Decimal) -> bool:
        position_base = state.get_position_base(symbol)
        entry_price = state.get_entry_price(symbol)
        position_side = state.get_position_side(symbol)
        if position_base <= 0 or entry_price <= 0:
            return False
        stop_loss_pct = self.config.stop_loss_pct_for_symbol(symbol)
        if position_side == "short":
            stop_price = entry_price * (Decimal("1") + stop_loss_pct)
            return current_price >= stop_price
        stop_price = entry_price * (Decimal("1") - stop_loss_pct)
        return current_price <= stop_price

    def take_profit_hit(self, state: BotState, symbol: str, current_price: Decimal) -> bool:
        position_base = state.get_position_base(symbol)
        entry_price = state.get_entry_price(symbol)
        position_side = state.get_position_side(symbol)
        if position_base <= 0 or entry_price <= 0:
            return False
        take_profit_pct = self.config.take_profit_pct_for_symbol(symbol)
        if position_side == "short":
            target_price = entry_price * (Decimal("1") - take_profit_pct)
            return current_price <= target_price
        target_price = entry_price * (Decimal("1") + take_profit_pct)
        return current_price >= target_price
