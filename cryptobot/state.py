"""In-memory bot state: portfolio, open position, and trade history.

Accounting uses a single margin model that covers spot and futures alike:

    notional = quantity * entry_price
    margin   = notional / leverage           (spot = 1x, so margin = notional)
    pnl      = direction * (price - entry) * quantity   (long: +1, short: -1)

Opening a position locks ``margin`` out of the free balance; closing it
returns the margin plus realized P&L. Equity is always
``free_balance + locked_margin + unrealized_pnl``. Spot long-only is just the
leverage-1, always-long special case of the same equations.

The state is shared between the trading engine (writer) and the web dashboard
(reader). All mutation happens on the engine thread under a lock; the web
layer only reads snapshots.
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
    """An open position, long or short, possibly leveraged."""
    side: str               # "long" | "short"
    quantity: float
    entry_price: float
    leverage: int
    margin: float           # collateral locked to hold this position
    opened_at: str = field(default_factory=_now)

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        return self.direction * (price - self.entry_price) * self.quantity

    def liquidation_price(self) -> float:
        """Isolated-margin liquidation price (ignores fees/maintenance margin).

        The position is wiped when its loss equals the posted margin, i.e. a
        1/leverage adverse move from entry.
        """
        move = self.entry_price / self.leverage
        return self.entry_price - move if self.side == "long" \
            else self.entry_price + move


@dataclass
class Trade:
    """A completed fill, recorded for the dashboard's history table."""
    action: str        # "open_long" | "close_long" | "open_short" | "close_short"
    side: str          # order side sent to the exchange: "buy" | "sell"
    quantity: float
    price: float
    reason: str        # signal / stop_loss / take_profit / liquidation / flip
    pnl: float = 0.0   # realized P&L, populated on close
    timestamp: str = field(default_factory=_now)


class BotState:
    def __init__(self, starting_balance: float, quote_currency: str = "USDT",
                 dry_run: bool = True, futures: bool = False,
                 leverage: int = 1) -> None:
        self._lock = threading.Lock()
        self.quote_currency = quote_currency
        self.dry_run = dry_run
        self.futures = futures
        self.leverage = leverage

        self.starting_equity = starting_balance
        self.quote_balance = starting_balance          # free collateral / cash
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
        """Total account value = free balance + locked margin + unrealized."""
        price = price if price is not None else self.last_price
        equity = self.quote_balance
        if self.position is not None:
            equity += self.position.margin
            if price:
                equity += self.position.unrealized_pnl(price)
        return equity

    # -- mutation (engine thread) -----------------------------------------
    def open_position(self, side: str, quantity: float, price: float,
                      margin: float, leverage: int, reason: str) -> None:
        with self._lock:
            self.quote_balance -= margin
            self.position = Position(side=side, quantity=quantity,
                                     entry_price=price, leverage=leverage,
                                     margin=margin)
            self.trades.appendleft(Trade(
                action=f"open_{side}",
                side="buy" if side == "long" else "sell",
                quantity=quantity, price=price, reason=reason))
            self.last_update = _now()

    def close_position(self, price: float, reason: str) -> float:
        """Close the open position at ``price``. Returns realized P&L.

        On liquidation the loss is capped at the posted margin (isolated
        margin): you cannot lose more than the collateral behind the position.
        """
        with self._lock:
            if self.position is None:
                return 0.0
            pos = self.position
            pnl = pos.unrealized_pnl(price)
            if reason == "liquidation":
                pnl = -pos.margin      # margin fully lost, nothing returned
            self.quote_balance += pos.margin + pnl
            self.realized_pnl += pnl
            self.trades.appendleft(Trade(
                action=f"close_{pos.side}",
                side="sell" if pos.side == "long" else "buy",
                quantity=pos.quantity, price=price, reason=reason, pnl=pnl))
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
                p = self.position
                position = {
                    "side": p.side,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "leverage": p.leverage,
                    "margin": p.margin,
                    "notional": p.notional,
                    "liquidation_price": p.liquidation_price(),
                    "opened_at": p.opened_at,
                    "unrealized_pnl": p.unrealized_pnl(price),
                }
            equity = self.equity(price)
            return {
                "status": self.status,
                "dry_run": self.dry_run,
                "futures": self.futures,
                "leverage": self.leverage,
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
