"""In-memory portfolio state for multi-symbol futures trading.

One ``PortfolioState`` holds a shared quote-currency balance (free margin) and
a ``SymbolState`` per traded asset. Accounting uses a single margin model that
covers spot and futures alike:

    notional = quantity * entry_price
    margin   = notional / leverage
    pnl      = direction * (price - entry) * quantity   (long +1, short -1)

Opening locks margin out of the shared balance; closing returns it plus
realized P&L. Equity = free balance + sum(locked margin + unrealized P&L).

All mutation happens on the engine thread under a lock; the web layer only
reads ``snapshot()``.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass
class Position:
    side: str               # "long" | "short"
    quantity: float
    entry_price: float      # actual fill price (includes slippage)
    leverage: int
    margin: float
    open_fee: float = 0.0    # fee paid to open, charged again to this trade at close
    opened_at: str = field(default_factory=lambda: _iso(_now()))

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        return self.direction * (price - self.entry_price) * self.quantity

    def liquidation_price(self) -> float:
        move = self.entry_price / self.leverage
        return self.entry_price - move if self.side == "long" \
            else self.entry_price + move


@dataclass
class Trade:
    base: str
    action: str        # open_long | close_long | open_short | close_short
    side: str          # exchange order side: buy | sell
    quantity: float
    price: float
    reason: str
    pnl: float = 0.0
    timestamp: str = field(default_factory=lambda: _iso(_now()))


class SymbolState:
    """Per-asset position, price history, and realized P&L."""

    def __init__(self, base: str, symbol: str) -> None:
        self.base = base
        self.symbol = symbol
        self.position: Optional[Position] = None
        self.last_price: float = 0.0
        self.realized_pnl: float = 0.0
        self.trades_count: int = 0
        self.price_history: Deque[tuple[str, float]] = deque(maxlen=240)


class PortfolioState:
    def __init__(self, bases: list[str], symbols: Dict[str, str],
                 starting_balance: float, quote_currency: str = "USDC",
                 dry_run: bool = True, futures: bool = False,
                 leverage: int = 1, trades_maxlen: int = 300,
                 equity_maxlen: int = 500, fee_rate: float = 0.0,
                 slippage_rate: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.quote_currency = quote_currency
        self.dry_run = dry_run
        self.futures = futures
        self.leverage = leverage

        # Cost model. Zero for live trading (the real exchange charges its own
        # fees on fills); set for backtests to model taker fees + slippage.
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.total_fees = 0.0

        self.starting_equity = starting_balance
        self.quote_balance = starting_balance          # shared free margin

        self.symbols: Dict[str, SymbolState] = {
            base: SymbolState(base, symbols[base]) for base in bases
        }

        # Larger buffers are used for backtests, which replay far more bars and
        # trades than a live session keeps on screen.
        self.trades: Deque[Trade] = deque(maxlen=trades_maxlen)
        self.realized_pnl = 0.0
        self.daily_pnl: Dict[str, float] = {}           # date -> realized P&L
        self.equity_curve: Deque[tuple[str, float]] = deque(maxlen=equity_maxlen)

        self.trading_halted = False
        self.strategy_name = ""
        self.status = "starting"
        self.last_update: str = _iso(_now())
        self.last_error: Optional[str] = None

    # -- equity ------------------------------------------------------------
    def _equity_locked(self) -> float:
        equity = self.quote_balance
        for st in self.symbols.values():
            if st.position is not None:
                equity += st.position.margin
                if st.last_price:
                    equity += st.position.unrealized_pnl(st.last_price)
        return equity

    def equity(self) -> float:
        with self._lock:
            return self._equity_locked()

    # -- mutation (engine thread) -----------------------------------------
    def set_price(self, base: str, price: float) -> None:
        with self._lock:
            st = self.symbols[base]
            st.last_price = price
            st.price_history.append((_iso(_now()), price))
            self.last_update = _iso(_now())

    def _fill_price(self, is_buy: bool, price: float) -> float:
        """Apply slippage: a buy fills higher, a sell lower (always adverse)."""
        s = self.slippage_rate
        return price * (1 + s) if is_buy else price * (1 - s)

    def open_position(self, base: str, side: str, quantity: float, price: float,
                      margin: float, leverage: int, reason: str) -> None:
        with self._lock:
            st = self.symbols[base]
            fill = self._fill_price(side == "long", price)   # open long = buy
            fee = quantity * fill * self.fee_rate
            self.quote_balance -= margin + fee
            self.total_fees += fee
            st.position = Position(side=side, quantity=quantity,
                                   entry_price=fill, leverage=leverage,
                                   margin=margin, open_fee=fee)
            st.trades_count += 1
            self.trades.appendleft(Trade(
                base=base, action=f"open_{side}",
                side="buy" if side == "long" else "sell",
                quantity=quantity, price=fill, reason=reason))
            self.last_update = _iso(_now())

    def close_position(self, base: str, price: float, reason: str) -> float:
        with self._lock:
            st = self.symbols[base]
            if st.position is None:
                return 0.0
            pos = st.position
            fill = self._fill_price(pos.side == "short", price)  # close short = buy
            if reason == "liquidation":
                gross = -pos.margin
            else:
                gross = pos.direction * (fill - pos.entry_price) * pos.quantity
            close_fee = pos.quantity * fill * self.fee_rate
            self.total_fees += close_fee
            # Net P&L charges this trade for both the open and close fees.
            pnl = gross - close_fee - pos.open_fee
            self.quote_balance += pos.margin + gross - close_fee
            self.realized_pnl += pnl
            st.realized_pnl += pnl
            st.trades_count += 1
            day = _now().strftime("%Y-%m-%d")
            self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + pnl
            self.trades.appendleft(Trade(
                base=base, action=f"close_{pos.side}",
                side="sell" if pos.side == "long" else "buy",
                quantity=pos.quantity, price=fill, reason=reason, pnl=pnl))
            st.position = None
            self.last_update = _iso(_now())
            return pnl

    def record_equity_point(self) -> None:
        with self._lock:
            self.equity_curve.append((_iso(_now()), self._equity_locked()))

    def set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.last_error = error
            self.last_update = _iso(_now())

    def set_halted(self, halted: bool) -> None:
        with self._lock:
            self.trading_halted = halted

    def set_strategy(self, name: str) -> None:
        with self._lock:
            self.strategy_name = name

    def update_meta(self, quote_currency: str, futures: bool,
                    leverage: int) -> None:
        """Reflect settings changes (quote/leverage) applied at runtime."""
        with self._lock:
            self.quote_currency = quote_currency
            self.futures = futures
            self.leverage = leverage

    # -- snapshot (web thread) --------------------------------------------
    def _position_view(self, pos: Position, price: float) -> dict[str, Any]:
        return {
            "side": pos.side, "quantity": pos.quantity,
            "entry_price": pos.entry_price, "leverage": pos.leverage,
            "margin": pos.margin, "notional": pos.notional,
            "liquidation_price": pos.liquidation_price(),
            "opened_at": pos.opened_at,
            "unrealized_pnl": pos.unrealized_pnl(price) if price else 0.0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            equity = self._equity_locked()
            today = _now().strftime("%Y-%m-%d")
            symbols = []
            open_positions = 0
            total_unrealized = 0.0
            for st in self.symbols.values():
                pos_view = None
                if st.position is not None:
                    open_positions += 1
                    pos_view = self._position_view(st.position, st.last_price)
                    total_unrealized += pos_view["unrealized_pnl"]
                symbols.append({
                    "base": st.base,
                    "symbol": st.symbol,
                    "last_price": st.last_price,
                    "position": pos_view,
                    "realized_pnl": st.realized_pnl,
                    "trades_count": st.trades_count,
                    "price_history": list(st.price_history),
                })
            return {
                "status": self.status,
                "dry_run": self.dry_run,
                "futures": self.futures,
                "leverage": self.leverage,
                "strategy": self.strategy_name,
                "trading_halted": self.trading_halted,
                "quote_currency": self.quote_currency,
                "starting_equity": self.starting_equity,
                "equity": equity,
                "quote_balance": self.quote_balance,
                "realized_pnl": self.realized_pnl,
                "total_fees": self.total_fees,
                "unrealized_pnl": total_unrealized,
                "total_pnl": equity - self.starting_equity,
                "total_return_pct": (
                    (equity - self.starting_equity) / self.starting_equity * 100
                    if self.starting_equity else 0.0
                ),
                "today_pnl": self.daily_pnl.get(today, 0.0),
                "open_positions": open_positions,
                "num_symbols": len(self.symbols),
                "daily_pnl": [{"date": d, "pnl": p}
                              for d, p in sorted(self.daily_pnl.items())],
                "equity_curve": [{"t": t, "equity": e}
                                 for t, e in self.equity_curve],
                "symbols": symbols,
                "trades": [vars(t) for t in list(self.trades)[:60]],
                "last_update": self.last_update,
                "last_error": self.last_error,
            }
