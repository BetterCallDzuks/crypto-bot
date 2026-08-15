"""In-memory bot state: portfolio, open position, and trade history.

The state is a single object shared between the trading engine (writer) and
the web dashboard (reader). All mutation happens on the engine thread; the web
layer only reads snapshots, so a lock is sufficient for consistency.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Position:
    """A single open long position in the base asset."""
    quantity: float
    entry_price: float
    opened_at: str = field(default_factory=_now)

    def value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.quantity


@dataclass
class Trade:
    """A completed fill, recorded for the dashboard's history table."""
    side: str          # "buy" | "sell"
    quantity: float
    price: float
    reason: str        # what triggered it (signal / stop_loss / take_profit)
    pnl: float = 0.0   # realized P&L, populated on the closing sell
    timestamp: str = field(default_factory=_now)


class BotState:
    def __init__(self, starting_balance: float, quote_currency: str = "USDT",
                 dry_run: bool = True) -> None:
        self._lock = threading.Lock()
        self.quote_currency = quote_currency
        self.dry_run = dry_run

        self.starting_equity = starting_balance
        self.quote_balance = starting_balance          # free quote currency
        self.position: Optional[Position] = None
        self.last_price: float = 0.0

        self.trades: Deque[Trade] = deque(maxlen=200)
        self.realized_pnl = 0.0
        self.trading_halted = False                    # daily-loss kill switch
        self.status = "starting"
        self.last_update: str = _now()
        self.last_error: Optional[str] = None

    # -- equity ------------------------------------------------------------
    def equity(self, price: float | None = None) -> float:
        """Total account value = free quote balance + open position value."""
        price = price if price is not None else self.last_price
        equity = self.quote_balance
        if self.position is not None and price:
            equity += self.position.value(price)
        return equity

    # -- mutation (engine thread) -----------------------------------------
    def record_buy(self, quantity: float, price: float, reason: str) -> None:
        with self._lock:
            self.quote_balance -= quantity * price
            self.position = Position(quantity=quantity, entry_price=price)
            self.trades.appendleft(
                Trade(side="buy", quantity=quantity, price=price, reason=reason)
            )
            self.last_update = _now()

    def record_sell(self, price: float, reason: str) -> float:
        """Close the open position at ``price``. Returns realized P&L."""
        with self._lock:
            if self.position is None:
                return 0.0
            pos = self.position
            proceeds = pos.quantity * price
            pnl = (price - pos.entry_price) * pos.quantity
            self.quote_balance += proceeds
            self.realized_pnl += pnl
            self.trades.appendleft(
                Trade(side="sell", quantity=pos.quantity, price=price,
                      reason=reason, pnl=pnl)
            )
            self.position = None
            self.last_update = _now()
            return pnl

    def set_price(self, price: float) -> None:
        with self._lock:
            self.last_price = price
            self.last_update = _now()

    def set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.last_error = error
            self.last_update = _now()

    def set_halted(self, halted: bool) -> None:
        with self._lock:
            self.trading_halted = halted

    # -- snapshot (web thread) --------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable view of current state for the dashboard."""
        with self._lock:
            price = self.last_price
            position = None
            if self.position is not None:
                position = {
                    "quantity": self.position.quantity,
                    "entry_price": self.position.entry_price,
                    "opened_at": self.position.opened_at,
                    "value": self.position.value(price),
                    "unrealized_pnl": self.position.unrealized_pnl(price),
                }
            equity = self.equity(price)
            return {
                "status": self.status,
                "dry_run": self.dry_run,
                "trading_halted": self.trading_halted,
                "quote_currency": self.quote_currency,
                "last_price": price,
                "quote_balance": self.quote_balance,
                "position": position,
                "equity": equity,
                "starting_equity": self.starting_equity,
                "realized_pnl": self.realized_pnl,
                "total_pnl": equity - self.starting_equity,
                "total_return_pct": (
                    (equity - self.starting_equity) / self.starting_equity * 100
                    if self.starting_equity else 0.0
                ),
                "last_update": self.last_update,
                "last_error": self.last_error,
                "trades": [vars(t) for t in list(self.trades)[:50]],
            }
