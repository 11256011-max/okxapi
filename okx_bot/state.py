from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class SymbolPosition:
    position_base: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    side: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "SymbolPosition":
        raw = raw or {}
        return cls(
            position_base=Decimal(str(raw.get("position_base", "0"))),
            entry_price=Decimal(str(raw.get("entry_price", "0"))),
            side=raw.get("side") or ("long" if Decimal(str(raw.get("position_base", "0"))) > 0 else None),
        )

    def to_json(self) -> dict[str, str | None]:
        return {
            "position_base": str(self.position_base),
            "entry_price": str(self.entry_price),
            "side": self.side,
        }


@dataclass
class BotState:
    day: str = field(default_factory=utc_date)
    daily_notional: Decimal = Decimal("0")
    daily_realized_pnl: Decimal = Decimal("0")
    positions: dict[str, SymbolPosition] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    default_symbol: str = "BTC/USDT"

    @classmethod
    def load(cls, path: str, default_symbol: str = "BTC/USDT") -> "BotState":
        file_path = Path(path)
        if not file_path.exists():
            state = cls(default_symbol=default_symbol)
            state.ensure_symbol(default_symbol)
            return state

        raw = json.loads(file_path.read_text(encoding="utf-8"))
        positions = cls.positions_from_raw(raw, default_symbol)
        state = cls(
            day=raw.get("day", utc_date()),
            daily_notional=Decimal(str(raw.get("daily_notional", "0"))),
            daily_realized_pnl=Decimal(str(raw.get("daily_realized_pnl", "0"))),
            positions=positions,
            trades=raw.get("trades", []),
            default_symbol=default_symbol,
        )
        state.ensure_symbol(default_symbol)
        state.reset_daily_if_needed()
        return state

    @staticmethod
    def positions_from_raw(raw: dict[str, Any], default_symbol: str) -> dict[str, SymbolPosition]:
        raw_positions = raw.get("positions")
        if isinstance(raw_positions, dict):
            return {
                symbol: SymbolPosition.from_raw(position)
                for symbol, position in raw_positions.items()
                if isinstance(position, dict)
            }

        # Backward compatibility for old single-symbol state.json files.
        migrated = SymbolPosition(
            position_base=Decimal(str(raw.get("position_base", "0"))),
            entry_price=Decimal(str(raw.get("entry_price", "0"))),
        )
        return {default_symbol: migrated}

    def save(self, path: str) -> None:
        file_path = Path(path)
        file_path.write_text(
            json.dumps(
                {
                    "day": self.day,
                    "daily_notional": str(self.daily_notional),
                    "daily_realized_pnl": str(self.daily_realized_pnl),
                    "positions": {
                        symbol: position.to_json()
                        for symbol, position in sorted(self.positions.items())
                    },
                    "trades": self.trades[-100:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def reset_daily_if_needed(self) -> None:
        today = utc_date()
        if self.day != today:
            self.day = today
            self.daily_notional = Decimal("0")
            self.daily_realized_pnl = Decimal("0")

    def ensure_symbol(self, symbol: str | None) -> SymbolPosition:
        key = symbol or self.default_symbol
        if key not in self.positions:
            self.positions[key] = SymbolPosition()
        return self.positions[key]

    def get_position_base(self, symbol: str | None = None) -> Decimal:
        return self.ensure_symbol(symbol).position_base

    def get_entry_price(self, symbol: str | None = None) -> Decimal:
        return self.ensure_symbol(symbol).entry_price

    def get_position_side(self, symbol: str | None = None) -> str | None:
        return self.ensure_symbol(symbol).side

    def record_trade(
        self,
        side: str,
        amount_base: Decimal,
        price: Decimal,
        quote_notional: Decimal,
        mode: str,
        order_id: str | None = None,
        symbol: str | None = None,
        position_side: str | None = None,
        reduce_only: bool = False,
        realized_pnl: Decimal = Decimal("0"),
    ) -> None:
        symbol = symbol or self.default_symbol
        position = self.ensure_symbol(symbol)
        self.reset_daily_if_needed()
        self.daily_notional += quote_notional
        self.trades.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "amount_base": str(amount_base),
                "price": str(price),
                "quote_notional": str(quote_notional),
                "mode": mode,
                "order_id": order_id,
                "position_side": position_side,
                "reduce_only": reduce_only,
                "realized_pnl": str(realized_pnl),
            }
        )
        self.daily_realized_pnl += realized_pnl

        if position_side in {"long", "short"} and not reduce_only:
            self.open_position(position, position_side, amount_base, price)
            return

        if reduce_only:
            position.position_base = max(Decimal("0"), position.position_base - amount_base)
            if position.position_base == 0:
                self.clear_symbol_position(symbol)
            return

        if side == "buy":
            position.side = "long"
            new_total_base = position.position_base + amount_base
            if new_total_base > 0:
                previous_cost = position.position_base * position.entry_price
                new_cost = amount_base * price
                position.entry_price = (previous_cost + new_cost) / new_total_base
            position.position_base = new_total_base
        elif side == "sell":
            position.position_base = max(Decimal("0"), position.position_base - amount_base)
            if position.position_base == 0:
                self.clear_symbol_position(symbol)

    @staticmethod
    def open_position(
        position: SymbolPosition,
        side: str,
        amount_base: Decimal,
        price: Decimal,
    ) -> None:
        new_total_base = position.position_base + amount_base
        if new_total_base > 0:
            previous_cost = position.position_base * position.entry_price
            new_cost = amount_base * price
            position.entry_price = (previous_cost + new_cost) / new_total_base
        position.position_base = new_total_base
        position.side = side

    def clear_symbol_position(self, symbol: str | None = None) -> None:
        position = self.ensure_symbol(symbol)
        position.position_base = Decimal("0")
        position.entry_price = Decimal("0")
        position.side = None
