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
from cryptobot.state import BotState
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

    if not config.trading.dry_run:
        kind = (f"FUTURES {config.futures.leverage}x {config.futures.margin_mode}"
                if config.futures.enabled else "SPOT")
        log.warning("=" * 68)
        log.warning("LIVE TRADING IS ENABLED — real orders with real funds.")
        log.warning("Mode: %s   Exchange: %s  sandbox=%s  symbol=%s",
                    kind, config.exchange.id, config.exchange.sandbox,
                    config.market.symbol)
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

    state = BotState(
        starting_balance=config.trading.paper_starting_balance,
        quote_currency=config.market.quote_currency,
        dry_run=config.trading.dry_run,
        futures=config.futures.enabled,
        leverage=config.futures.leverage if config.futures.enabled else 1,
    )
    engine = TradingEngine(config, exchange, state)
    engine.start()

    app = create_app(state, engine)
    log.info("Dashboard: http://%s:%d", config.web.host, config.web.port)
    try:
        app.run(host=config.web.host, port=config.web.port,
                debug=False, use_reloader=False)
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
