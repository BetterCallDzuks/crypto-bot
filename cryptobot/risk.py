"""Risk management: position sizing and exit/guard rules.

Pure, deterministic functions that sit between a strategy signal and an order.
All exit rules are side-aware so they work identically for long and short
positions. Sizing accounts for leverage: the configured fraction of balance is
posted as *margin*, and the position's notional is that margin times leverage.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    position_size_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_daily_loss_pct: float


def margin_to_use(balance: float, limits: RiskLimits) -> float:
    """Collateral to post for a new position (a fraction of free balance)."""
    if balance <= 0:
        return 0.0
    return balance * limits.position_size_pct


def position_quantity(balance: float, price: float, leverage: int,
                      limits: RiskLimits) -> float:
    """Base-asset quantity for a new position, given leverage.

    margin  = balance * position_size_pct
    notional = margin * leverage
    quantity = notional / price
    """
    if balance <= 0 or price <= 0 or leverage < 1:
        return 0.0
    notional = margin_to_use(balance, limits) * leverage
    return notional / price


def should_stop_loss(side: str, entry_price: float, current_price: float,
                     limits: RiskLimits) -> bool:
    """True when an adverse move has reached the stop-loss threshold.

    The stop is measured on price distance from entry, so with leverage it is
    hit well before liquidation — that is the point of it.
    """
    if entry_price <= 0:
        return False
    if side == "long":
        return current_price <= entry_price * (1 - limits.stop_loss_pct)
    return current_price >= entry_price * (1 + limits.stop_loss_pct)


def should_take_profit(side: str, entry_price: float, current_price: float,
                       limits: RiskLimits) -> bool:
    """True when a favorable move has reached the take-profit threshold."""
    if entry_price <= 0:
        return False
    if side == "long":
        return current_price >= entry_price * (1 + limits.take_profit_pct)
    return current_price <= entry_price * (1 - limits.take_profit_pct)


def should_liquidate(side: str, entry_price: float, current_price: float,
                     leverage: int) -> bool:
    """True when price has reached the isolated-margin liquidation level.

    Approximate: ignores fees and maintenance margin. A backstop only — the
    stop-loss should normally fire long before this on any sane config.
    """
    if entry_price <= 0 or leverage < 1:
        return False
    move = entry_price / leverage
    if side == "long":
        return current_price <= entry_price - move
    return current_price >= entry_price + move


def daily_loss_limit_hit(starting_equity: float, current_equity: float,
                         limits: RiskLimits) -> bool:
    """True once the day's drawdown reaches the configured maximum.

    While this is true the engine blocks new entries (a kill switch for the
    trading day); an open position may still be closed by its exits.
    """
    if starting_equity <= 0:
        return False
    max_loss = starting_equity * limits.max_daily_loss_pct
    return (starting_equity - current_equity) >= max_loss
