"""The trading engine.

Runs a loop on a background thread:

    1. Pull recent closing prices.
    2. Check risk exits (liquidation / stop-loss / take-profit) on any position.
    3. Ask the strategy for a signal and act on it, subject to risk limits.
    4. Update shared state and sleep until the next poll.

Directionality depends on config:
  * Futures (allow_short): BUY goes/flips long, SELL goes/flips short.
  * Spot / long-only:      BUY opens long, SELL closes it (never shorts).

Every order funnels through the exchange client, whose dry-run gate decides
whether it is real, and every decision funnels through the risk module.
"""

from __future__ import annotations

import logging
import threading

from .config import Config
from .risk import (
    RiskLimits,
    daily_loss_limit_hit,
    margin_to_use,
    position_quantity,
    should_liquidate,
    should_stop_loss,
    should_take_profit,
)
from .state import BotState
from .strategy import Signal, build_strategy

log = logging.getLogger("cryptobot.trader")


class TradingEngine:
    def __init__(self, config: Config, exchange, state: BotState) -> None:
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
        self.futures = config.futures.enabled
        self.allow_short = config.futures.enabled and config.futures.allow_short
        self.leverage = config.futures.leverage if config.futures.enabled else 1
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
        kind = f"futures {self.leverage}x" if self.futures else "spot"
        log.info("Trading engine started in %s mode — %s on %s",
                 mode, kind, self.config.market.symbol)
        self.state.set_status(f"running ({mode}, {kind})")
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
        if self.state.position is not None and self._check_exits(price):
            return

        # 2. Daily loss guard: block new entries once tripped.
        halted = daily_loss_limit_hit(
            self.state.starting_equity, self.state.equity(price), self.limits
        )
        self.state.set_halted(halted)

        # 3. Strategy signal -> directional action.
        signal = self.strategy.evaluate(closes)
        if signal is Signal.BUY:
            self._go_long(price, halted)
        elif signal is Signal.SELL:
            self._go_short_or_flat(price, halted)

    # -- exits -------------------------------------------------------------
    def _check_exits(self, price: float) -> bool:
        pos = self.state.position
        assert pos is not None
        if should_liquidate(pos.side, pos.entry_price, price, pos.leverage):
            self._close(price, reason="liquidation")
            return True
        if should_stop_loss(pos.side, pos.entry_price, price, self.limits):
            self._close(price, reason="stop_loss")
            return True
        if should_take_profit(pos.side, pos.entry_price, price, self.limits):
            self._close(price, reason="take_profit")
            return True
        return False

    # -- directional actions ----------------------------------------------
    def _go_long(self, price: float, halted: bool) -> None:
        pos = self.state.position
        if pos is not None and pos.side == "long":
            return                                  # already long, hold
        if pos is not None and pos.side == "short":
            self._close(price, reason="flip")       # close short before flip
        if not halted:
            self._open("long", price)

    def _go_short_or_flat(self, price: float, halted: bool) -> None:
        pos = self.state.position
        if pos is not None and pos.side == "short":
            return                                  # already short, hold
        if pos is not None and pos.side == "long":
            self._close(price, reason="signal")     # close the long
        if self.allow_short and not halted:
            self._open("short", price)              # and flip short (futures)

    # -- order helpers -----------------------------------------------------
    def _open(self, side: str, price: float) -> None:
        balance = self.state.quote_balance
        qty = position_quantity(balance, price, self.leverage, self.limits)
        if qty <= 0:
            return
        margin = margin_to_use(balance, self.limits)
        order_side = "buy" if side == "long" else "sell"
        self.exchange.create_order(order_side, qty, price, reduce_only=False)
        self.state.open_position(side, qty, price, margin, self.leverage,
                                 reason="signal")
        log.info("OPEN %s %.8f @ %.2f (%dx, margin=%.2f)",
                 side.upper(), qty, price, self.leverage, margin)

    def _close(self, price: float, reason: str) -> None:
        pos = self.state.position
        if pos is None:
            return
        order_side = "sell" if pos.side == "long" else "buy"
        self.exchange.create_order(order_side, pos.quantity, price,
                                   reduce_only=True)
        pnl = self.state.close_position(price, reason=reason)
        log.info("CLOSE %s %.8f @ %.2f (%s) pnl=%.2f",
                 pos.side.upper(), pos.quantity, price, reason, pnl)
