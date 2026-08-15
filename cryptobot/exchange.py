"""Exchange access via ccxt, with a hard dry-run boundary.

``ExchangeClient`` wraps a ccxt exchange for the two things the engine needs:
reading recent candles and placing market orders. Market-data reads always hit
the real exchange (public, no funds at risk). Order placement is gated: when
``dry_run`` is true no order is ever transmitted — the method returns a
simulated fill so the rest of the system behaves identically in paper mode.
"""

from __future__ import annotations

from typing import Any

import ccxt

from .config import Config


class ExchangeClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.symbol = config.market.symbol
        self.timeframe = config.market.timeframe
        self.dry_run = config.trading.dry_run
        self._exchange = self._build_exchange(config)

    @staticmethod
    def _build_exchange(config: Config) -> ccxt.Exchange:
        ex_cfg = config.exchange
        try:
            exchange_class = getattr(ccxt, ex_cfg.id)
        except AttributeError as exc:
            raise ValueError(
                f"Unknown exchange id '{ex_cfg.id}'. See ccxt.exchanges for "
                f"the supported list."
            ) from exc

        params: dict[str, Any] = {"enableRateLimit": True}
        if ex_cfg.api_key and ex_cfg.api_secret:
            params["apiKey"] = ex_cfg.api_key
            params["secret"] = ex_cfg.api_secret
            if ex_cfg.api_password:
                params["password"] = ex_cfg.api_password

        exchange = exchange_class(params)

        if ex_cfg.sandbox:
            # Not every exchange offers a sandbox; fail loudly if it doesn't
            # so the user isn't silently pointed at production.
            try:
                exchange.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                raise RuntimeError(
                    f"Exchange '{ex_cfg.id}' does not support sandbox mode. "
                    f"Set exchange.sandbox: false to use production endpoints "
                    f"(only do this when you understand the risk)."
                ) from exc
        return exchange

    # -- market data (always live) ----------------------------------------
    def fetch_closes(self, limit: int) -> list[float]:
        """Return the most recent ``limit`` closing prices, oldest first."""
        ohlcv = self._exchange.fetch_ohlcv(
            self.symbol, timeframe=self.timeframe, limit=limit
        )
        # ccxt OHLCV row: [timestamp, open, high, low, close, volume]
        return [row[4] for row in ohlcv]

    def fetch_price(self) -> float:
        """Return the latest traded price for the configured symbol."""
        ticker = self._exchange.fetch_ticker(self.symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"No price available for {self.symbol}")
        return float(price)

    # -- orders (gated by dry_run) ----------------------------------------
    def create_market_buy(self, quantity: float, price: float) -> dict[str, Any]:
        return self._order("buy", quantity, price)

    def create_market_sell(self, quantity: float, price: float) -> dict[str, Any]:
        return self._order("sell", quantity, price)

    def _order(self, side: str, quantity: float, price: float) -> dict[str, Any]:
        if self.dry_run:
            # Simulated fill — nothing leaves the process.
            return {
                "simulated": True,
                "side": side,
                "symbol": self.symbol,
                "amount": quantity,
                "price": price,
            }
        order = self._exchange.create_order(
            symbol=self.symbol, type="market", side=side, amount=quantity
        )
        order["simulated"] = False
        return order
