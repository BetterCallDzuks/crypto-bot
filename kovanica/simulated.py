"""A self-contained simulated multi-symbol market, for demos and offline tests.

Implements the same interface the engine expects from ``ExchangeClient``
(``fetch_closes`` / ``fetch_price`` / ``create_order``, all keyed by base
asset), but generates each asset's prices with an independent random walk.
Lets the whole bot run — engine, dashboard, charts, trades — with no network
access and no API keys. Orders are always simulated; nothing leaves the
process, so there is never any financial risk in this mode.
"""

from __future__ import annotations

import random
from typing import Any

from .config import Config

# Rough starting prices so charts look plausible per asset.
_START_PRICES = {
    "BTC": 65_000.0, "ETH": 3_200.0, "XRP": 0.55, "SOL": 150.0,
    "DOGE": 0.14, "BNB": 580.0, "ADA": 0.45, "AVAX": 35.0, "LINK": 18.0,
    "MATIC": 0.9, "DOT": 7.0, "LTC": 85.0, "TRX": 0.12,
}


class _Series:
    def __init__(self, price: float, rng: random.Random) -> None:
        self._rng = rng
        self._price = price
        self.closes = [price]
        # Seed enough history that any strategy (incl. the confluence ensemble)
        # is warm on the first tick.
        for _ in range(250):
            self.step()

    def step(self) -> float:
        drift = self._rng.uniform(-0.004, 0.004)
        shock = self._rng.gauss(0, 0.003)
        self._price *= max(0.01, 1 + drift + shock)
        self.closes.append(self._price)
        if len(self.closes) > 500:
            self.closes = self.closes[-500:]
        return self._price


class SimulatedExchange:
    def __init__(self, config: Config, seed: int | None = None) -> None:
        self.config = config
        self.dry_run = True
        rng = random.Random(seed)
        self._series: dict[str, _Series] = {}
        for base in config.market.symbols:
            start = _START_PRICES.get(base, 100.0)
            # Independent RNG stream per asset so they diverge.
            self._series[base] = _Series(start, random.Random(rng.random()))

    # -- market data ------------------------------------------------------
    def fetch_closes(self, base: str, limit: int) -> list[float]:
        s = self._series[base]
        s.step()
        return s.closes[-limit:]

    def fetch_price(self, base: str) -> float:
        return self._series[base].closes[-1]

    # -- orders (always simulated) ----------------------------------------
    def create_order(self, base: str, side: str, quantity: float, price: float,
                     reduce_only: bool = False) -> dict[str, Any]:
        return {"simulated": True, "base": base, "side": side,
                "amount": quantity, "price": price, "reduceOnly": reduce_only}
