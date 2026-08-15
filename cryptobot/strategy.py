"""Trading strategies.

A strategy consumes a series of closing prices and emits a discrete ``Signal``.
Strategies are pure and stateless: they receive all the data they need and
return a decision, which makes them trivial to unit test. Position and risk
handling live elsewhere (risk.py, trader.py).

Included strategies (all operate on close prices only):

  sma_crossover   Trend      Fast/slow simple-MA crossover.
  ema_crossover   Trend      Fast/slow exponential-MA crossover (more reactive).
  macd            Momentum   MACD line crossing its signal line.
  rsi             Reversion  Enter as RSI leaves oversold / overbought.
  bollinger       Reversion  Price re-entering the Bollinger bands.
  donchian        Breakout   Close breaking the N-bar high/low (Turtle-style).
  confluence      Ensemble   Trades only when several of the above agree.

No strategy is guaranteed to be profitable; these are well-known building
blocks, not financial advice. The confluence ensemble is the most robust
default because it demands agreement before committing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


# ---------------------------------------------------------------------------
# Indicator helpers (compact lists: no leading None, aligned to recent bars)
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values, or None."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """EMA seeded with the SMA of the first window.

    Returns a compact list whose last element is the current EMA and whose
    ``[-2]`` is the previous one (empty if there isn't enough data).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_series(values: Sequence[float], period: int) -> list[float]:
    """Wilder's RSI as a compact list (last element = current RSI)."""
    if period < 2:
        raise ValueError("rsi period must be >= 2")
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period

    def rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out = [rsi(avg_gain, avg_loss)]
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out.append(rsi(avg_gain, avg_loss))
    return out


def _crossed(prev_a: float, prev_b: float, now_a: float, now_b: float):
    """Return (crossed_up, crossed_down) for series a relative to series b."""
    up = prev_a <= prev_b and now_a > now_b
    down = prev_a >= prev_b and now_a < now_b
    return up, down


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
class SmaCrossoverStrategy:
    """Fast/slow simple-moving-average crossover (trend following)."""

    KEY = "sma_crossover"

    def __init__(self, fast_period: int = 9, slow_period: int = 21) -> None:
        fast_period, slow_period = int(fast_period), int(slow_period)
        if fast_period < 1 or slow_period < 1:
            raise ValueError("periods must be >= 1")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def warmup(self) -> int:
        return self.slow_period + 1

    def evaluate(self, closes: Sequence[float]) -> Signal:
        if len(closes) < self.warmup:
            return Signal.HOLD
        prev = closes[:-1]
        up, down = _crossed(sma(prev, self.fast_period), sma(prev, self.slow_period),
                            sma(closes, self.fast_period), sma(closes, self.slow_period))
        return Signal.BUY if up else Signal.SELL if down else Signal.HOLD


class EmaCrossoverStrategy:
    """Fast/slow exponential-moving-average crossover (trend following)."""

    KEY = "ema_crossover"

    def __init__(self, fast_period: int = 12, slow_period: int = 26) -> None:
        fast_period, slow_period = int(fast_period), int(slow_period)
        if fast_period < 1 or slow_period < 1:
            raise ValueError("periods must be >= 1")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def warmup(self) -> int:
        return self.slow_period + 2

    def evaluate(self, closes: Sequence[float]) -> Signal:
        fast = ema_series(closes, self.fast_period)
        slow = ema_series(closes, self.slow_period)
        if len(fast) < 2 or len(slow) < 2:
            return Signal.HOLD
        up, down = _crossed(fast[-2], slow[-2], fast[-1], slow[-1])
        return Signal.BUY if up else Signal.SELL if down else Signal.HOLD


class MacdStrategy:
    """MACD line crossing its signal line (momentum)."""

    KEY = "macd"

    def __init__(self, fast_period: int = 12, slow_period: int = 26,
                 signal_period: int = 9) -> None:
        fast_period = int(fast_period)
        slow_period = int(slow_period)
        signal_period = int(signal_period)
        if fast_period >= slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        if signal_period < 1:
            raise ValueError("signal_period must be >= 1")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period

    @property
    def warmup(self) -> int:
        return self.slow_period + self.signal_period + 2

    def evaluate(self, closes: Sequence[float]) -> Signal:
        fast = ema_series(closes, self.fast_period)
        slow = ema_series(closes, self.slow_period)
        n = min(len(fast), len(slow))
        if n < 2:
            return Signal.HOLD
        macd = [fast[-n + i] - slow[-n + i] for i in range(n)]
        signal = ema_series(macd, self.signal_period)
        if len(signal) < 2:
            return Signal.HOLD
        up, down = _crossed(macd[-2], signal[-2], macd[-1], signal[-1])
        return Signal.BUY if up else Signal.SELL if down else Signal.HOLD


class RsiStrategy:
    """Enter as RSI crosses back out of oversold / overbought (reversion)."""

    KEY = "rsi"

    def __init__(self, period: int = 14, oversold: float = 30.0,
                 overbought: float = 70.0) -> None:
        period = int(period)
        oversold, overbought = float(oversold), float(overbought)
        if period < 2:
            raise ValueError("rsi period must be >= 2")
        if not 0 < oversold < overbought < 100:
            raise ValueError("require 0 < oversold < overbought < 100")
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    @property
    def warmup(self) -> int:
        return self.period + 2

    def evaluate(self, closes: Sequence[float]) -> Signal:
        r = rsi_series(closes, self.period)
        if len(r) < 2:
            return Signal.HOLD
        prev, now = r[-2], r[-1]
        if prev <= self.oversold and now > self.oversold:
            return Signal.BUY
        if prev >= self.overbought and now < self.overbought:
            return Signal.SELL
        return Signal.HOLD


class BollingerStrategy:
    """Price re-entering the Bollinger bands from outside (reversion)."""

    KEY = "bollinger"

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        period = int(period)
        num_std = float(num_std)
        if period < 2:
            raise ValueError("bollinger period must be >= 2")
        if num_std <= 0:
            raise ValueError("num_std must be > 0")
        self.period = period
        self.num_std = num_std

    @property
    def warmup(self) -> int:
        return self.period + 2

    def _bands(self, window: Sequence[float]) -> tuple[float, float]:
        m = sum(window) / self.period
        var = sum((x - m) ** 2 for x in window) / self.period
        sd = var ** 0.5
        return m + self.num_std * sd, m - self.num_std * sd

    def evaluate(self, closes: Sequence[float]) -> Signal:
        if len(closes) < self.period + 1:
            return Signal.HOLD
        up_now, lo_now = self._bands(closes[-self.period:])
        up_prev, lo_prev = self._bands(closes[-self.period - 1:-1])
        c_now, c_prev = closes[-1], closes[-2]
        if c_prev <= lo_prev and c_now > lo_now:
            return Signal.BUY
        if c_prev >= up_prev and c_now < up_now:
            return Signal.SELL
        return Signal.HOLD


class DonchianBreakoutStrategy:
    """Close breaking the prior N-bar high/low (Turtle-style breakout)."""

    KEY = "donchian"

    def __init__(self, period: int = 20) -> None:
        period = int(period)
        if period < 1:
            raise ValueError("donchian period must be >= 1")
        self.period = period

    @property
    def warmup(self) -> int:
        return self.period + 1

    def evaluate(self, closes: Sequence[float]) -> Signal:
        if len(closes) < self.period + 1:
            return Signal.HOLD
        prior = closes[-self.period - 1:-1]
        c = closes[-1]
        if c > max(prior):
            return Signal.BUY
        if c < min(prior):
            return Signal.SELL
        return Signal.HOLD


class ConfluenceStrategy:
    """Vote across several strategies; act only when ``threshold`` agree.

    Members are EMA crossover, MACD, and RSI (each with its defaults). Requiring
    agreement filters out many false signals from any single indicator, at the
    cost of trading less often — usually the most robust choice.
    """

    KEY = "confluence"

    def __init__(self, threshold: int = 2) -> None:
        threshold = int(threshold)
        if not 1 <= threshold <= 3:
            raise ValueError("confluence threshold must be between 1 and 3")
        self.threshold = threshold
        self.members = [EmaCrossoverStrategy(), MacdStrategy(), RsiStrategy()]

    @property
    def warmup(self) -> int:
        return max(m.warmup for m in self.members)

    def evaluate(self, closes: Sequence[float]) -> Signal:
        votes = 0
        for m in self.members:
            s = m.evaluate(closes)
            votes += 1 if s is Signal.BUY else -1 if s is Signal.SELL else 0
        if votes >= self.threshold:
            return Signal.BUY
        if votes <= -self.threshold:
            return Signal.SELL
        return Signal.HOLD


# ---------------------------------------------------------------------------
# Registry + factory
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategySpec:
    key: str
    label: str
    description: str
    factory: Callable[..., object]
    default_params: dict


REGISTRY: dict[str, StrategySpec] = {
    "confluence": StrategySpec(
        "confluence", "Confluence (ensemble)",
        "Trades only when several strategies agree — the most robust default.",
        ConfluenceStrategy, {"threshold": 2}),
    "ema_crossover": StrategySpec(
        "ema_crossover", "EMA crossover",
        "Fast/slow exponential moving averages; reacts quicker than SMA.",
        EmaCrossoverStrategy, {"fast_period": 12, "slow_period": 26}),
    "sma_crossover": StrategySpec(
        "sma_crossover", "SMA crossover",
        "Fast/slow simple moving averages; classic trend following.",
        SmaCrossoverStrategy, {"fast_period": 9, "slow_period": 21}),
    "macd": StrategySpec(
        "macd", "MACD",
        "MACD line crossing its signal line; momentum.",
        MacdStrategy, {"fast_period": 12, "slow_period": 26, "signal_period": 9}),
    "rsi": StrategySpec(
        "rsi", "RSI reversion",
        "Enters as RSI leaves oversold/overbought; mean reversion.",
        RsiStrategy, {"period": 14, "oversold": 30, "overbought": 70}),
    "bollinger": StrategySpec(
        "bollinger", "Bollinger Bands",
        "Buys/sells as price re-enters the bands; mean reversion.",
        BollingerStrategy, {"period": 20, "num_std": 2.0}),
    "donchian": StrategySpec(
        "donchian", "Donchian breakout",
        "Breakout of the N-bar high/low; Turtle-style trend capture.",
        DonchianBreakoutStrategy, {"period": 20}),
}


def available_strategies() -> dict[str, dict]:
    """UI-facing catalogue: key -> {label, description, params(defaults)}."""
    return {
        spec.key: {
            "label": spec.label,
            "description": spec.description,
            "params": dict(spec.default_params),
        }
        for spec in REGISTRY.values()
    }


def build_strategy(name: str, params: dict | None = None, **kwargs):
    """Construct a strategy by name, merging defaults < params < kwargs."""
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {sorted(REGISTRY)}"
        )
    spec = REGISTRY[name]
    merged = {**spec.default_params, **(params or {}), **kwargs}
    return spec.factory(**merged)
