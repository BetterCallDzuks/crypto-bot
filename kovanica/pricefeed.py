"""Live price feed over Binance futures WebSocket streams.

Subscribes to per-symbol mark-price updates (`<symbol>@markPrice@1s`) and keeps
the latest price for each asset in memory. The engine uses this for real-time
risk checks (stop-loss / liquidation on the live mark price, not a 30s-old
candle) and the dashboard shows live prices.

Binance-specific and opt-in (`market.live_feed`). If the library or connection
is unavailable, the bot simply falls back to REST polling — the feed never
blocks trading.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Dict, Optional

log = logging.getLogger("kovanica.pricefeed")

_PROD_WS = "wss://fstream.binance.com/stream"
_TESTNET_WS = "wss://stream.binancefuture.com/stream"


def stream_symbol(base: str, quote: str) -> str:
    """Binance stream symbol for a perpetual, e.g. BTC + USDC -> 'btcusdc'."""
    return f"{base}{quote}".lower()


class LivePriceFeed:
    def __init__(self, bases: list[str], quote: str, sandbox: bool = False,
                 on_price: Optional[Callable[[str, float], None]] = None) -> None:
        # base -> stream symbol, and the reverse map keyed by Binance's SYMBOL.
        self._stream_symbols = {b: stream_symbol(b, quote) for b in bases}
        self._by_symbol = {s.upper(): b for b, s in self._stream_symbols.items()}
        self._ws_base = _TESTNET_WS if sandbox else _PROD_WS
        self._on_price = on_price
        self._prices: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self.connected = False

    # -- pure parsing (unit-tested without a network) ---------------------
    def parse_message(self, message: str) -> Dict[str, float]:
        """Extract {base: price} updates from a combined-stream message."""
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return {}
        payload = data.get("data", data)
        symbol = payload.get("s")
        price = payload.get("p")
        if symbol is None or price is None:
            return {}
        base = self._by_symbol.get(str(symbol).upper())
        if base is None:
            return {}
        try:
            return {base: float(price)}
        except (ValueError, TypeError):
            return {}

    def _apply(self, message: str) -> None:
        for base, price in self.parse_message(message).items():
            with self._lock:
                self._prices[base] = price
            if self._on_price:
                try:
                    self._on_price(base, price)
                except Exception:  # noqa: BLE001 - a bad callback must not kill the feed
                    log.exception("price callback failed")

    # -- accessors ---------------------------------------------------------
    def get(self, base: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(base)

    def url(self) -> str:
        streams = "/".join(f"{s}@markPrice@1s"
                           for s in self._stream_symbols.values())
        return f"{self._ws_base}?streams={streams}"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="price-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.connected = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            log.warning("websocket-client not installed — live feed disabled; "
                        "falling back to REST polling.")
            return

        def on_message(ws, message):
            self._apply(message)

        def on_open(ws):
            self.connected = True
            log.info("Live price feed connected (%d symbols).",
                     len(self._stream_symbols))

        def on_close(ws, *args):
            self.connected = False

        def on_error(ws, error):
            self.connected = False
            log.warning("Live feed error: %s", error)

        # Reconnect loop with backoff until asked to stop.
        backoff = 1
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.url(), on_message=on_message, on_open=on_open,
                    on_close=on_close, on_error=on_error)
                self._ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                log.warning("Live feed crashed: %s", exc)
            self.connected = False
            if self._stop.wait(min(backoff, 30)):
                break
            backoff = min(backoff * 2, 30)
