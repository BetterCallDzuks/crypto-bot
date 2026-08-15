"""Trading strategies.

A strategy consumes a series of closing prices and emits a discrete
``Signal``. Strategies are intentionally pure and stateless: they receive all
the data they need and return a decision, which makes them trivial to unit
test and to reason about. Position/risk handling lives elsewhere (risk.py).
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values.

    Returns ``None`` when there is not enough data yet.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


class SmaCrossoverStrategy:
    """Classic fast/slow moving-average crossover.

    * BUY  when the fast SMA crosses from at-or-below to above the slow SMA.
    * SELL when the fast SMA crosses from at-or-above to below the slow SMA.
    * HOLD otherwise (including while warming up on insufficient data).

    Acting on the *crossover* (a change in relationship) rather than the
    current relationship avoids re-emitting the same signal every tick.
    """

    def __init__(self, fast_period: int, slow_period: int) -> None:
        if fast_period >= slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def warmup(self) -> int:
        """Number of closes required before a signal can be produced."""
        # Need one extra sample to compare the previous relationship.
        return self.slow_period + 1

    def evaluate(self, closes: Sequence[float]) -> Signal:
        if len(closes) < self.warmup:
            return Signal.HOLD

        prev = closes[:-1]
        fast_now = sma(closes, self.fast_period)
        slow_now = sma(closes, self.slow_period)
        fast_prev = sma(prev, self.fast_period)
        slow_prev = sma(prev, self.slow_period)

        # All four are guaranteed non-None given the warmup check above.
        assert None not in (fast_now, slow_now, fast_prev, slow_prev)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            return Signal.BUY
        if crossed_down:
            return Signal.SELL
        return Signal.HOLD


def build_strategy(name: str, **params) -> SmaCrossoverStrategy:
    """Factory mapping a config name to a strategy instance."""
    strategies = {
        "sma_crossover": lambda: SmaCrossoverStrategy(
            fast_period=params["fast_period"],
            slow_period=params["slow_period"],
        ),
    }
    if name not in strategies:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {sorted(strategies)}"
        )
    return strategies[name]()
