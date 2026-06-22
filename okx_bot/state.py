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
class BotState:
    day: str = field(default_factory=utc_date)
    daily_notional: Decimal = Decimal("0")
    position_base: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    protective_algo_id: str | None = None
    protective_algo_cl_ord_id: str | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "BotState":
        file_path = Path(path)
        if not file_path.exists():
            return cls()

        raw = json.loads(file_path.read_text(encoding="utf-8"))
        state = cls(
            day=raw.get("day", utc_date()),
            daily_notional=Decimal(str(raw.get("daily_notional", "0"))),
            position_base=Decimal(str(raw.get("position_base", "0"))),
            entry_price=Decimal(str(raw.get("entry_price", "0"))),
            protective_algo_id=raw.get("protective_algo_id") or None,
            protective_algo_cl_ord_id=raw.get("protective_algo_cl_ord_id") or None,
            trades=raw.get("trades", []),
        )
        state.reset_daily_if_needed()
        return state

    def save(self, path: str) -> None:
        file_path = Path(path)
        file_path.write_text(
            json.dumps(
                {
                    "day": self.day,
                    "daily_notional": str(self.daily_notional),
                    "position_base": str(self.position_base),
                    "entry_price": str(self.entry_price),
                    "protective_algo_id": self.protective_algo_id,
                    "protective_algo_cl_ord_id": self.protective_algo_cl_ord_id,
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

    def record_trade(
        self,
        side: str,
        amount_base: Decimal,
        price: Decimal,
        quote_notional: Decimal,
        mode: str,
        order_id: str | None = None,
    ) -> None:
        self.reset_daily_if_needed()
        self.daily_notional += quote_notional
        self.trades.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "side": side,
                "amount_base": str(amount_base),
                "price": str(price),
                "quote_notional": str(quote_notional),
                "mode": mode,
                "order_id": order_id,
            }
        )

        if side == "buy":
            new_total_base = self.position_base + amount_base
            if new_total_base > 0:
                previous_cost = self.position_base * self.entry_price
                new_cost = amount_base * price
                self.entry_price = (previous_cost + new_cost) / new_total_base
            self.position_base = new_total_base
        elif side == "sell":
            self.position_base = max(Decimal("0"), self.position_base - amount_base)
            if self.position_base == 0:
                self.entry_price = Decimal("0")
                self.clear_protective_order()

    def set_protective_order(self, algo_id: str | None, algo_cl_ord_id: str | None) -> None:
        self.protective_algo_id = algo_id
        self.protective_algo_cl_ord_id = algo_cl_ord_id

    def clear_protective_order(self) -> None:
        self.protective_algo_id = None
        self.protective_algo_cl_ord_id = None
