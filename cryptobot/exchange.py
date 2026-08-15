"""Exchange access via ccxt, with a hard dry-run boundary.

``ExchangeClient`` wraps a ccxt exchange for what the engine needs: reading
recent candles and placing market orders. Market-data reads always hit the
real exchange (public, no funds at risk). Order placement is gated: when
``dry_run`` is true no order is ever transmitted — a simulated fill is returned
so the rest of the system behaves identically in paper mode.

When ``futures.enabled`` the client selects the exchange's perpetual-swap
market type and configures leverage and margin mode, so the same code drives
spot or leveraged futures based only on config.
"""

from __future__ import annotations

import logging
from typing import Any

import ccxt

from .config import Config

log = logging.getLogger("cryptobot.exchange")


class ExchangeClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.symbol = config.market.symbol
        self.timeframe = config.market.timeframe
        self.dry_run = config.trading.dry_run
        self.futures = config.futures.enabled
        self._exchange = self._build_exchange(config)
        if self.futures:
            self._configure_futures(config)

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

        params: dict[str, Any] = {"enableRateLimit": True, "options": {}}
        if config.futures.enabled:
            # Trade perpetual swaps (the common crypto "futures" product).
            params["options"]["defaultType"] = "swap"
        if ex_cfg.api_key and ex_cfg.api_secret:
            params["apiKey"] = ex_cfg.api_key
            params["secret"] = ex_cfg.api_secret
            if ex_cfg.api_password:
                params["password"] = ex_cfg.api_password

        exchange = exchange_class(params)

        if ex_cfg.sandbox:
            try:
                exchange.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                raise RuntimeError(
                    f"Exchange '{ex_cfg.id}' does not support sandbox mode. "
                    f"Set exchange.sandbox: false to use production endpoints "
                    f"(only do this when you understand the risk)."
                ) from exc
        return exchange

    def _configure_futures(self, config: Config) -> None:
        """Set leverage and margin mode on the exchange.

        Skipped in dry-run (no authenticated session). Failures are logged, not
        fatal: some exchanges reject these calls in sandbox or want them set
        per-order; the engine still sizes positions by the configured leverage.
        """
        if self.dry_run:
            return
        f = config.futures
        try:
            self._exchange.set_margin_mode(f.margin_mode, self.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set margin mode (%s): %s", f.margin_mode, exc)
        try:
            self._exchange.set_leverage(f.leverage, self.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set leverage (%dx): %s", f.leverage, exc)

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
    def create_order(self, side: str, quantity: float, price: float,
                     reduce_only: bool = False) -> dict[str, Any]:
        """Place a market order. ``side`` is the raw exchange side (buy/sell).

        For futures, opening a short is a ``sell`` and closing it a ``buy``;
        ``reduce_only`` marks a closing order so it can only shrink a position.
        """
        if self.dry_run:
            return {
                "simulated": True, "side": side, "symbol": self.symbol,
                "amount": quantity, "price": price, "reduceOnly": reduce_only,
            }
        params: dict[str, Any] = {}
        if self.futures and reduce_only:
            params["reduceOnly"] = True
        order = self._exchange.create_order(
            symbol=self.symbol, type="market", side=side,
            amount=quantity, params=params,
        )
        order["simulated"] = False
        return order
