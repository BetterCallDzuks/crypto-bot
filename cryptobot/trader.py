"""The multi-symbol trading engine.

Each poll, the engine walks every configured asset:

    1. Pull recent closing prices for the asset.
    2. Check risk exits (liquidation / stop-loss / take-profit) on its position.
    3. Ask the strategy for a signal and act, subject to portfolio risk limits.

The daily-loss kill switch and position sizing are portfolio-level: they use
the shared quote balance and total equity across all assets. Strategy, risk,
leverage, and directionality are read from config and can be reloaded at
runtime when the dashboard's settings page saves changes.
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
from .state import PortfolioState
from .strategy import REGISTRY, Signal, build_strategy

log = logging.getLogger("cryptobot.trader")


class TradingEngine:
    def __init__(self, config: Config, exchange, state: PortfolioState) -> None:
        self.config = config
        self.exchange = exchange
        self.state = state
        self.bases = list(state.symbols.keys())
        self._settings_lock = threading.Lock()
        self.reload_from_config()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- runtime settings --------------------------------------------------
    def reload_from_config(self) -> None:
        """(Re)build strategy and risk parameters from the current config.

        Called on startup and whenever the settings page saves. Changes to
        strategy/risk/leverage/direction take effect on the next poll; the set
        of traded symbols and the quote currency require a restart.
        """
        c = self.config
        with self._settings_lock:
            self.strategy = build_strategy(c.strategy.name, c.strategy.params)
            self.limits = RiskLimits(
                position_size_pct=c.risk.position_size_pct,
                stop_loss_pct=c.risk.stop_loss_pct,
                take_profit_pct=c.risk.take_profit_pct,
                max_daily_loss_pct=c.risk.max_daily_loss_pct,
            )
            self.futures = c.futures.enabled
            self.allow_short = c.futures.enabled and c.futures.allow_short
            self.leverage = c.futures.leverage if c.futures.enabled else 1
        label = REGISTRY[c.strategy.name].label if c.strategy.name in REGISTRY \
            else c.strategy.name
        self.state.set_strategy(label)
        log.info("Settings (re)loaded: strategy=%s, %dx leverage, size %.0f%%, "
                 "stop %.1f%% / tp %.1f%%",
                 label, self.leverage,
                 self.limits.position_size_pct * 100,
                 self.limits.stop_loss_pct * 100, self.limits.take_profit_pct * 100)

    def _settings(self):
        with self._settings_lock:
            return (self.strategy, self.limits, self.leverage, self.allow_short)

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
        log.info("Engine started in %s mode — %s, %d symbols: %s",
                 mode, kind, len(self.bases), ", ".join(self.bases))
        self.state.set_status(f"running ({mode}, {kind})")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.exception("Error in trading loop")
                self.state.set_status("error", error=str(exc))
            self._stop.wait(self.config.trading.poll_interval)

    # -- one iteration (the unit-testable seam) ---------------------------
    def tick(self) -> None:
        strategy, limits, leverage, allow_short = self._settings()

        # Portfolio-level daily-loss guard, evaluated once per poll.
        halted = daily_loss_limit_hit(
            self.state.starting_equity, self.state.equity(), limits
        )
        self.state.set_halted(halted)

        for base in self.bases:
            self._process(base, strategy, limits, leverage, allow_short, halted)

        self.state.record_equity_point()

    def _process(self, base, strategy, limits, leverage, allow_short, halted):
        closes = self.exchange.fetch_closes(base, limit=strategy.warmup + 5)
        if not closes:
            return
        price = closes[-1]
        self.state.set_price(base, price)

        pos = self.state.symbols[base].position
        if pos is not None and self._check_exits(base, pos, price, limits):
            return

        signal = strategy.evaluate(closes)
        if signal is Signal.BUY:
            self._go_long(base, price, leverage, limits, halted)
        elif signal is Signal.SELL:
            self._go_short_or_flat(base, price, leverage, limits,
                                   allow_short, halted)

    # -- exits -------------------------------------------------------------
    def _check_exits(self, base, pos, price, limits) -> bool:
        if should_liquidate(pos.side, pos.entry_price, price, pos.leverage):
            self._close(base, price, "liquidation")
            return True
        if should_stop_loss(pos.side, pos.entry_price, price, limits):
            self._close(base, price, "stop_loss")
            return True
        if should_take_profit(pos.side, pos.entry_price, price, limits):
            self._close(base, price, "take_profit")
            return True
        return False

    # -- directional actions ----------------------------------------------
    def _go_long(self, base, price, leverage, limits, halted):
        pos = self.state.symbols[base].position
        if pos is not None and pos.side == "long":
            return
        if pos is not None and pos.side == "short":
            self._close(base, price, "flip")
        if not halted:
            self._open(base, "long", price, leverage, limits)

    def _go_short_or_flat(self, base, price, leverage, limits, allow_short, halted):
        pos = self.state.symbols[base].position
        if pos is not None and pos.side == "short":
            return
        if pos is not None and pos.side == "long":
            self._close(base, price, "signal")
        if allow_short and not halted:
            self._open(base, "short", price, leverage, limits)

    # -- order helpers -----------------------------------------------------
    def _open(self, base, side, price, leverage, limits):
        balance = self.state.quote_balance
        qty = position_quantity(balance, price, leverage, limits)
        if qty <= 0:
            return
        margin = margin_to_use(balance, limits)
        order_side = "buy" if side == "long" else "sell"
        self.exchange.create_order(base, order_side, qty, price,
                                   reduce_only=False)
        self.state.open_position(base, side, qty, price, margin, leverage,
                                 reason="signal")
        log.info("[%s] OPEN %s %.6f @ %.4f (%dx, margin=%.2f)",
                 base, side.upper(), qty, price, leverage, margin)

    def _close(self, base, price, reason):
        pos = self.state.symbols[base].position
        if pos is None:
            return
        order_side = "sell" if pos.side == "long" else "buy"
        self.exchange.create_order(base, order_side, pos.quantity, price,
                                   reduce_only=True)
        pnl = self.state.close_position(base, price, reason=reason)
        log.info("[%s] CLOSE %s %.6f @ %.4f (%s) pnl=%.2f",
                 base, pos.side.upper(), pos.quantity, price, reason, pnl)
