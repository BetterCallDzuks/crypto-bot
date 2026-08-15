"""Risk management: position sizing and exit/guard rules.

This module encodes the capital-preservation logic that sits between a raw
strategy signal and an actual order. It is pure and deterministic so the rules
can be unit tested exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    position_size_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_daily_loss_pct: float


def position_size(quote_balance: float, price: float,
                  limits: RiskLimits) -> float:
    """Return the base-asset quantity to buy for a new position.

    Deploys ``position_size_pct`` of the available quote balance at ``price``.
    Returns 0 when inputs are non-positive (nothing to trade).
    """
    if quote_balance <= 0 or price <= 0:
        return 0.0
    quote_to_spend = quote_balance * limits.position_size_pct
    return quote_to_spend / price


def should_stop_loss(entry_price: float, current_price: float,
                     limits: RiskLimits) -> bool:
    """True when the position has fallen to/through the stop-loss level."""
    if entry_price <= 0:
        return False
    stop_level = entry_price * (1 - limits.stop_loss_pct)
    return current_price <= stop_level


def should_take_profit(entry_price: float, current_price: float,
                       limits: RiskLimits) -> bool:
    """True when the position has risen to/through the take-profit level."""
    if entry_price <= 0:
        return False
    target = entry_price * (1 + limits.take_profit_pct)
    return current_price >= target


def daily_loss_limit_hit(starting_equity: float, current_equity: float,
                         limits: RiskLimits) -> bool:
    """True once the day's drawdown reaches the configured maximum.

    While this is true the engine blocks new entries (a kill switch for the
    trading day); existing positions may still be closed by their exits.
    """
    if starting_equity <= 0:
        return False
    max_loss = starting_equity * limits.max_daily_loss_pct
    return (starting_equity - current_equity) >= max_loss
