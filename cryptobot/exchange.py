"""Exchange access via ccxt, multi-symbol, with a hard dry-run boundary.

``ExchangeClient`` wraps a ccxt exchange for the whole basket: reading recent
candles and placing market orders per asset. Market-data reads always hit the
real exchange (public). Order placement is gated — when ``dry_run`` is true no
order is transmitted; a simulated fill is returned so paper mode behaves
identically.

For futures the client selects the perpetual-swap market type and, in live
mode, configures leverage and margin mode for every traded symbol.
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
        self.timeframe = config.market.timeframe
        self.dry_run = config.trading.dry_run
        self.futures = config.futures.enabled
        # base -> ccxt market symbol
        self.symbols = {b: config.market_symbol(b) for b in config.market.symbols}
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
                f"Unknown exchange id '{ex_cfg.id}'. See ccxt.exchanges."
            ) from exc

        params: dict[str, Any] = {"enableRateLimit": True, "options": {}}
        if config.futures.enabled:
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
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Exchange '{ex_cfg.id}' does not support sandbox mode. "
                    f"Set exchange.sandbox: false to use production endpoints "
                    f"(only do this when you understand the risk)."
                ) from exc
        return exchange

    def _configure_futures(self, config: Config) -> None:
        """Set leverage and margin mode for each symbol (live mode only)."""
        if self.dry_run:
            return
        f = config.futures
        for symbol in self.symbols.values():
            try:
                self._exchange.set_margin_mode(f.margin_mode, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not set margin mode: %s", symbol, exc)
            try:
                self._exchange.set_leverage(f.leverage, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not set leverage: %s", symbol, exc)

    # -- market data (always live) ----------------------------------------
    def fetch_closes(self, base: str, limit: int) -> list[float]:
        symbol = self.symbols[base]
        ohlcv = self._exchange.fetch_ohlcv(
            symbol, timeframe=self.timeframe, limit=limit
        )
        return [row[4] for row in ohlcv]

    def fetch_price(self, base: str) -> float:
        symbol = self.symbols[base]
        ticker = self._exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"No price available for {symbol}")
        return float(price)

    # -- orders (gated by dry_run) ----------------------------------------
    def create_order(self, base: str, side: str, quantity: float, price: float,
                     reduce_only: bool = False) -> dict[str, Any]:
        symbol = self.symbols[base]
        if self.dry_run:
            return {
                "simulated": True, "side": side, "symbol": symbol,
                "amount": quantity, "price": price, "reduceOnly": reduce_only,
            }
        params: dict[str, Any] = {}
        if self.futures and reduce_only:
            params["reduceOnly"] = True
        order = self._exchange.create_order(
            symbol=symbol, type="market", side=side,
            amount=quantity, params=params,
        )
        order["simulated"] = False
        return order
