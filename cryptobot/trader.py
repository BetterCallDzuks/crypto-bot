"""The trading engine.

Runs a loop on a background thread:

    1. Pull recent closing prices.
    2. Check risk-based exits (stop-loss / take-profit) on any open position.
    3. Ask the strategy for a signal and act on it, subject to risk limits.
    4. Update shared state and sleep until the next poll.

Every decision funnels through the risk module, and every order funnels
through ``ExchangeClient`` whose dry-run gate decides whether it is real.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .exchange import ExchangeClient
from .risk import (
    RiskLimits,
    daily_loss_limit_hit,
    position_size,
    should_stop_loss,
    should_take_profit,
)
from .state import BotState
from .strategy import Signal, build_strategy

log = logging.getLogger("cryptobot.trader")


class TradingEngine:
    def __init__(self, config: Config, exchange: ExchangeClient,
                 state: BotState) -> None:
        self.config = config
        self.exchange = exchange
        self.state = state
        self.strategy = build_strategy(
            config.strategy.name,
            fast_period=config.strategy.fast_period,
            slow_period=config.strategy.slow_period,
        )
        self.limits = RiskLimits(
            position_size_pct=config.risk.position_size_pct,
            stop_loss_pct=config.risk.stop_loss_pct,
            take_profit_pct=config.risk.take_profit_pct,
            max_daily_loss_pct=config.risk.max_daily_loss_pct,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="trading-engine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.config.trading.poll_interval + 5)
        self.state.set_status("stopped")

    def _run(self) -> None:
        mode = "DRY-RUN (paper)" if self.config.trading.dry_run else "LIVE"
        log.info("Trading engine started in %s mode on %s",
                 mode, self.config.market.symbol)
        self.state.set_status(f"running ({mode})")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.exception("Error in trading loop")
                self.state.set_status("error", error=str(exc))
            self._stop.wait(self.config.trading.poll_interval)

    # -- one iteration (also the unit-testable seam) ----------------------
    def tick(self) -> None:
        closes = self.exchange.fetch_closes(limit=self.strategy.warmup + 5)
        if not closes:
            return
        price = closes[-1]
        self.state.set_price(price)

        # 1. Risk exits take priority over any new strategy signal.
        if self.state.position is not None:
            if self._check_exits(price):
                return

        # 2. Daily loss guard: block new entries once tripped.
        halted = daily_loss_limit_hit(
            self.state.starting_equity, self.state.equity(price), self.limits
        )
        self.state.set_halted(halted)

        # 3. Strategy signal.
        signal = self.strategy.evaluate(closes)

        if signal is Signal.BUY and self.state.position is None and not halted:
            self._open_position(price)
        elif signal is Signal.SELL and self.state.position is not None:
            self._close_position(price, reason="signal")

    # -- actions -----------------------------------------------------------
    def _check_exits(self, price: float) -> bool:
        pos = self.state.position
        assert pos is not None
        if should_stop_loss(pos.entry_price, price, self.limits):
            self._close_position(price, reason="stop_loss")
            return True
        if should_take_profit(pos.entry_price, price, self.limits):
            self._close_position(price, reason="take_profit")
            return True
        return False

    def _open_position(self, price: float) -> None:
        qty = position_size(self.state.quote_balance, price, self.limits)
        if qty <= 0:
            return
        self.exchange.create_market_buy(qty, price)
        self.state.record_buy(qty, price, reason="signal")
        log.info("BUY %.8f @ %.2f", qty, price)

    def _close_position(self, price: float, reason: str) -> None:
        pos = self.state.position
        if pos is None:
            return
        self.exchange.create_market_sell(pos.quantity, price)
        pnl = self.state.record_sell(price, reason=reason)
        log.info("SELL %.8f @ %.2f (%s) pnl=%.2f",
                 pos.quantity, price, reason, pnl)
