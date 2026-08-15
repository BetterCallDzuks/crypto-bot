"""A self-contained simulated market, for demos and offline testing.

Implements the same duck-typed interface the trading engine expects from
``ExchangeClient`` (``fetch_closes`` / ``fetch_price`` / ``create_market_*``),
but generates prices with a random walk instead of hitting a real exchange.
This lets you run the whole bot — engine, dashboard, trades — with no network
access and no API keys. Orders are always simulated; nothing leaves the
process, so there is never any financial risk in this mode.
"""

from __future__ import annotations

import random
from typing import Any

from .config import Config


class SimulatedExchange:
    def __init__(self, config: Config, start_price: float = 30_000.0,
                 seed: int | None = None) -> None:
        self.config = config
        self.symbol = config.market.symbol
        self.dry_run = True  # simulated market is always paper
        self._rng = random.Random(seed)
        self._price = start_price
        # Seed enough history that the strategy is warm on the first tick.
        self._closes: list[float] = [start_price]
        for _ in range(60):
            self._step()

    def _step(self) -> float:
        """Advance the price one candle via a gently trending random walk."""
        # Small drift that occasionally flips sign creates the up/down swings
        # a crossover strategy needs, without runaway prices.
        drift = self._rng.uniform(-0.004, 0.004)
        shock = self._rng.gauss(0, 0.003)
        self._price *= max(0.5, 1 + drift + shock)
        self._closes.append(self._price)
        # Bound memory; keep well more than any warmup window.
        if len(self._closes) > 500:
            self._closes = self._closes[-500:]
        return self._price

    # -- market data ------------------------------------------------------
    def fetch_closes(self, limit: int) -> list[float]:
        self._step()
        return self._closes[-limit:]

    def fetch_price(self) -> float:
        return self._price

    # -- orders (always simulated) ----------------------------------------
    def create_order(self, side: str, quantity: float, price: float,
                     reduce_only: bool = False) -> dict[str, Any]:
        return {"simulated": True, "side": side, "amount": quantity,
                "price": price, "reduceOnly": reduce_only}
