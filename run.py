#!/usr/bin/env python3
"""crypto-bot entrypoint.

Wires the pieces together and starts both the trading engine (background
thread) and the web dashboard (foreground). Run with:

    python run.py

Configuration is read from config.yaml and .env. See the README for the full
safety walkthrough before enabling live trading.
"""

from __future__ import annotations

import logging
import sys

from cryptobot.config import load_config
from cryptobot.exchange import ExchangeClient
from cryptobot.state import PortfolioState
from cryptobot.trader import TradingEngine
from cryptobot.web.app import create_app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("cryptobot")

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - user-facing startup error
        log.error("Configuration error: %s", exc)
        return 1

    bases = list(config.market.symbols)
    symbols = {b: config.market_symbol(b) for b in bases}

    if not config.trading.dry_run:
        kind = (f"FUTURES {config.futures.leverage}x {config.futures.margin_mode}"
                if config.futures.enabled else "SPOT")
        log.warning("=" * 68)
        log.warning("LIVE TRADING IS ENABLED — real orders with real funds.")
        log.warning("Mode: %s   Exchange: %s  sandbox=%s",
                    kind, config.exchange.id, config.exchange.sandbox)
        log.warning("Quote: %s   Symbols: %s",
                    config.market.quote_currency, ", ".join(symbols.values()))
        if config.futures.enabled:
            log.warning("Leverage amplifies losses; positions can be LIQUIDATED.")
        log.warning("=" * 68)

    try:
        if config.market.source == "simulated":
            from cryptobot.simulated import SimulatedExchange
            log.info("Using SIMULATED market data (offline, no network/keys).")
            exchange = SimulatedExchange(config)
        else:
            exchange = ExchangeClient(config)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to initialize exchange: %s", exc)
        return 1

    state = PortfolioState(
        bases=bases,
        symbols=symbols,
        starting_balance=config.trading.paper_starting_balance,
        quote_currency=config.market.quote_currency,
        dry_run=config.trading.dry_run,
        futures=config.futures.enabled,
        leverage=config.futures.leverage if config.futures.enabled else 1,
    )

    # Live WebSocket price feed (Binance only, real data only, opt-in).
    price_feed = None
    if (config.market.source == "exchange" and config.market.live_feed
            and config.exchange.id == "binance"):
        from cryptobot.pricefeed import LivePriceFeed
        price_feed = LivePriceFeed(bases, config.market.quote_currency,
                                   sandbox=config.exchange.sandbox)
        log.info("Live WebSocket price feed enabled (real-time risk checks).")

    engine = TradingEngine(config, exchange, state, price_feed=price_feed)
    engine.start()

    app = create_app(config, state, engine)
    if config.auth_active:
        log.info("Dashboard login is ENABLED (user: %s).", config.auth.username)
        if config.auth.ephemeral_secret:
            log.info("No SECRET_KEY set — sessions reset on restart. Set "
                     "SECRET_KEY in .env to keep logins across restarts.")
    elif config.host_is_local:
        log.warning("Dashboard has NO login (localhost only). Set "
                    "DASHBOARD_PASSWORD in .env before exposing it (Tailscale).")
    log.info("Dashboard: http://%s:%d  (%d symbols, quote %s)",
             config.web.host, config.web.port, len(bases),
             config.market.quote_currency)
    try:
        app.run(host=config.web.host, port=config.web.port,
                debug=False, use_reloader=False, threaded=True)
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
