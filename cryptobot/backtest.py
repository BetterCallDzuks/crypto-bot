"""Backtesting: replay historical candles through the *live* engine.

The point of this module is fidelity. Instead of re-implementing the trading
rules, it feeds historical price windows to the real ``TradingEngine`` and
``PortfolioState`` exactly the way the live bot is fed — a fixed sliding window
per tick — so a backtest exercises the same strategy, sizing, stop-loss,
take-profit, liquidation and flip logic that runs with real money.

Data comes from the exchange (ccxt history) or the offline simulator. Results
are summarized with the metrics that actually matter for judging a strategy:
return, max drawdown, win rate, profit factor, and a risk-adjusted ratio.

It models a configurable taker fee and slippage on every fill (defaults from
``config.backtest``), so returns are net of trading costs.

Caveats (read these): a backtest describes the past on one data set. It still
does not model funding payments or order-book depth (large orders would slip
more than a flat rate), and the daily-loss kill switch is disabled during
replay (it's a live-ops guardrail, not a strategy property). A good backtest is
necessary but never sufficient — it is not a promise of profit.
"""

from __future__ import annotations

import copy
import logging
import math
import random
import statistics
from typing import Any, Dict, List

from .config import Config
from .state import PortfolioState
from .strategy import REGISTRY
from .trader import TradingEngine

log = logging.getLogger("cryptobot.backtest")

# Approximate start prices for the offline simulator (mirrors simulated.py).
_START_PRICES = {
    "BTC": 65_000.0, "ETH": 3_200.0, "XRP": 0.55, "SOL": 150.0,
    "DOGE": 0.14, "BNB": 580.0, "ADA": 0.45, "AVAX": 35.0, "LINK": 18.0,
    "MATIC": 0.9, "DOT": 7.0, "LTC": 85.0, "TRX": 0.12,
}

_TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}


class ReplayExchange:
    """Serves a growing prefix of pre-loaded history, keyed by base asset."""

    def __init__(self, history: Dict[str, List[float]]) -> None:
        self.history = history
        self.dry_run = True
        self._idx = 0

    def set_index(self, idx: int) -> None:
        self._idx = idx

    def fetch_closes(self, base: str, limit: int) -> List[float]:
        return self.history[base][:self._idx][-limit:]

    def fetch_price(self, base: str) -> float:
        return self.history[base][:self._idx][-1]

    def create_order(self, *args, **kwargs) -> dict:
        return {"simulated": True}


# ---------------------------------------------------------------------------
# History sources
# ---------------------------------------------------------------------------
def generate_sim_history(config: Config, bars: int,
                         seed: int | None = None) -> Dict[str, List[float]]:
    """Random-walk history per symbol (offline; no network)."""
    rng = random.Random(seed)
    hist: Dict[str, List[float]] = {}
    for base in config.market.symbols:
        r = random.Random(rng.random())
        price = _START_PRICES.get(base, 100.0)
        closes = [price]
        for _ in range(bars - 1):
            drift = r.uniform(-0.004, 0.004)
            shock = r.gauss(0, 0.003)
            price *= max(0.01, 1 + drift + shock)
            closes.append(price)
        hist[base] = closes
    return hist


def fetch_history(config: Config, bars: int) -> Dict[str, List[float]]:
    """Historical closes per symbol from the exchange via ccxt (paginated)."""
    closes, _ = _fetch_ohlcv(config, bars)
    return closes


def _fetch_ohlcv(config: Config, bars: int
                 ) -> tuple[Dict[str, List[float]], Dict[str, List[int]]]:
    """Return (closes, open-timestamps-ms) per symbol from the exchange."""
    from .exchange import ExchangeClient
    client = ExchangeClient(config)
    ex = client._exchange
    tf = config.market.timeframe
    tf_ms = ex.parse_timeframe(tf) * 1000
    closes: Dict[str, List[float]] = {}
    times: Dict[str, List[int]] = {}
    for base, symbol in client.symbols.items():
        since = ex.milliseconds() - bars * tf_ms
        rows: list = []
        while len(rows) < bars:
            batch = ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=1000)
            if not batch:
                break
            rows += batch
            since = batch[-1][0] + tf_ms
            if len(batch) < 1000:
                break
        rows = rows[-bars:]
        closes[base] = [r[4] for r in rows]
        times[base] = [r[0] for r in rows]
    return closes, times


def align_funding(timestamps: List[int],
                  events: List[tuple[int, float]]) -> List[float]:
    """Map funding events onto candle bars by timestamp (pure, testable).

    Each funding event (ms, rate) is attached to the first bar at or after its
    time, so replaying the schedule charges funding on the bar where it would
    actually have settled. Returns a per-bar list of summed rates (0 elsewhere).
    """
    schedule = [0.0] * len(timestamps)
    if not timestamps or not events:
        return schedule
    events = sorted(events)
    ei = 0
    for i, ts in enumerate(timestamps):
        prev = timestamps[i - 1] if i > 0 else None
        while ei < len(events) and events[ei][0] <= ts:
            if prev is None or events[ei][0] > prev:
                schedule[i] += events[ei][1]
            ei += 1
    return schedule


def build_funding_schedule(config: Config, timestamps: Dict[str, List[int]]
                           ) -> Dict[str, List[float]]:
    """Fetch real historical funding rates and align them to the candle bars."""
    from .exchange import ExchangeClient
    client = ExchangeClient(config)
    ex = client._exchange
    schedule: Dict[str, List[float]] = {}
    for base, symbol in client.symbols.items():
        ts = timestamps.get(base, [])
        try:
            events = _fetch_funding_events(ex, symbol, ts[0] if ts else None)
        except Exception as exc:  # noqa: BLE001 - degrade to no funding
            log.warning("[%s] funding history unavailable: %s", symbol, exc)
            events = []
        schedule[base] = align_funding(ts, events)
    return schedule


def _fetch_funding_events(ex, symbol: str, since_ms: int | None
                          ) -> List[tuple[int, float]]:
    if since_ms is None:
        return []
    out: list = []
    since = since_ms
    while True:
        batch = ex.fetch_funding_rate_history(symbol, since=since, limit=1000)
        if not batch:
            break
        out += batch
        since = batch[-1]["timestamp"] + 1
        if len(batch) < 1000:
            break
    return [(e["timestamp"], e["fundingRate"]) for e in out
            if e.get("fundingRate") is not None]


def load_history(config: Config, bars: int,
                 seed: int | None = None) -> Dict[str, List[float]]:
    if config.market.source == "simulated":
        return generate_sim_history(config, bars, seed=seed)
    return fetch_history(config, bars)


def load_market(config: Config, bars: int, seed: int | None = None
                ) -> tuple[Dict[str, List[float]], Dict[str, List[float]] | None]:
    """Load closes and, for exchange data, a real per-bar funding schedule.

    Returns ``(closes, funding_schedule)``. ``funding_schedule`` is ``None`` for
    the simulated source (which has no real funding — the flat model is used).
    """
    if config.market.source == "simulated":
        return generate_sim_history(config, bars, seed=seed), None
    closes, times = _fetch_ohlcv(config, bars)
    return closes, build_funding_schedule(config, times)


# ---------------------------------------------------------------------------
# Backtest run + metrics
# ---------------------------------------------------------------------------
def run_backtest(config: Config, history: Dict[str, List[float]],
                 fee_rate: float | None = None,
                 slippage_rate: float | None = None,
                 funding_rate: float | None = None,
                 funding_schedule: Dict[str, List[float]] | None = None) -> dict:
    """Replay ``history`` through the engine and return metrics + equity curve.

    Works on a copy of ``config`` with the daily-loss kill switch disabled so
    the run measures the strategy and its exits over the full period. Taker
    fees, slippage, and perpetual funding are modeled (defaults from
    ``config.backtest``). When ``funding_schedule`` is given (real per-bar,
    per-symbol historical rates), it overrides the flat ``funding_rate``.
    """
    cfg = copy.deepcopy(config)
    cfg.risk.max_daily_loss_pct = 1.0            # disable daily halt for replay
    cfg.trading.dry_run = True
    fee_rate = cfg.backtest.fee_rate if fee_rate is None else fee_rate
    slippage_rate = (cfg.backtest.slippage_rate if slippage_rate is None
                     else slippage_rate)
    funding_rate = (cfg.backtest.funding_rate if funding_rate is None
                    else funding_rate)

    bases = [b for b in cfg.market.symbols if history.get(b)]
    if not bases:
        raise ValueError("no history to backtest")
    symbols = {b: cfg.market_symbol(b) for b in bases}
    length = min(len(history[b]) for b in bases)

    state = PortfolioState(
        bases=bases, symbols=symbols,
        starting_balance=cfg.trading.paper_starting_balance,
        quote_currency=cfg.market.quote_currency, dry_run=True,
        futures=cfg.futures.enabled,
        leverage=cfg.futures.leverage if cfg.futures.enabled else 1,
        trades_maxlen=10_000_000, equity_maxlen=10_000_000,
        fee_rate=fee_rate, slippage_rate=slippage_rate,
    )
    replay = ReplayExchange({b: history[b][:length] for b in bases})
    engine = TradingEngine(cfg, replay, state)      # thread never started

    use_real_funding = funding_schedule is not None
    # Flat model: funding is charged every N bars from the interval + timeframe.
    tf_minutes = _TIMEFRAME_MINUTES.get(cfg.market.timeframe, 1)
    funding_bars = max(1, round(cfg.backtest.funding_interval_hours * 60
                                / tf_minutes))

    start = engine.strategy.warmup + 5
    for step, idx in enumerate(range(start, length + 1)):
        replay.set_index(idx)
        engine.tick()
        if use_real_funding:
            bar = idx - 1
            rates = {b: funding_schedule[b][bar]
                     for b in bases
                     if bar < len(funding_schedule.get(b, [])) and
                     funding_schedule[b][bar]}
            if rates:
                state.apply_funding(rates)
        elif funding_rate and step > 0 and step % funding_bars == 0:
            state.apply_funding(funding_rate)

    # Close any positions still open at the final bar so P&L is fully realized.
    for base in bases:
        if state.symbols[base].position is not None:
            state.close_position(base, history[base][length - 1],
                                 reason="backtest_end")

    metrics = _metrics(state, cfg.market.timeframe)
    metrics["bars"] = length
    metrics["symbols"] = bases
    metrics["strategy"] = cfg.strategy.name
    metrics["leverage"] = state.leverage
    metrics["fees_paid"] = state.total_fees
    metrics["funding_paid"] = state.total_funding
    metrics["fee_rate"] = fee_rate
    metrics["slippage_rate"] = slippage_rate
    metrics["funding_rate"] = funding_rate
    metrics["funding_source"] = "historical" if use_real_funding else "flat"
    metrics["equity_curve"] = _downsample([e for _, e in state.equity_curve], 300)
    return metrics


def _metrics(state: PortfolioState, timeframe: str) -> dict:
    curve = [e for _, e in state.equity_curve]
    start = state.starting_equity
    final = curve[-1] if curve else state.equity()

    closes = [t for t in state.trades if t.action.startswith("close")]
    pnls = [t.pnl for t in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win = sum(wins)
    gross_loss = sum(losses)                      # negative
    win_rate = len(wins) / n * 100 if n else 0.0
    if gross_loss < 0:
        profit_factor = gross_win / abs(gross_loss)
    else:
        profit_factor = math.inf if gross_win > 0 else 0.0

    # Max drawdown from the equity curve.
    peak = -math.inf
    max_dd = 0.0
    for e in curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, (e - peak) / peak)

    # Risk-adjusted ratio: annualized Sharpe from per-bar returns (rf = 0).
    rets = [curve[i] / curve[i - 1] - 1
            for i in range(1, len(curve)) if curve[i - 1] > 0]
    sharpe = 0.0
    if len(rets) > 1:
        mu = statistics.mean(rets)
        sd = statistics.pstdev(rets)
        if sd > 0:
            minutes = _TIMEFRAME_MINUTES.get(timeframe, 1)
            periods_per_year = 525_600 / minutes
            sharpe = mu / sd * math.sqrt(periods_per_year)

    return {
        "starting_equity": start,
        "final_equity": final,
        "total_return_pct": (final - start) / start * 100 if start else 0.0,
        "realized_pnl": state.realized_pnl,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "profit_factor": (None if profit_factor == math.inf else profit_factor),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "best_trade": max(pnls) if pnls else 0.0,
        "worst_trade": min(pnls) if pnls else 0.0,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
    }


def compare_strategies(config: Config, history: Dict[str, List[float]],
                       fee_rate: float | None = None,
                       slippage_rate: float | None = None,
                       funding_rate: float | None = None,
                       funding_schedule: Dict[str, List[float]] | None = None
                       ) -> List[dict]:
    """Backtest every registered strategy on the same history; rank by return."""
    results = []
    for name in REGISTRY:
        cfg = copy.deepcopy(config)
        cfg.strategy.name = name
        cfg.strategy.params = {}
        try:
            m = run_backtest(cfg, history, fee_rate=fee_rate,
                             slippage_rate=slippage_rate, funding_rate=funding_rate,
                             funding_schedule=funding_schedule)
            m["label"] = REGISTRY[name].label
            results.append(m)
        except Exception as exc:  # noqa: BLE001 - keep comparing the rest
            log.warning("Backtest failed for %s: %s", name, exc)
    results.sort(key=lambda r: r["total_return_pct"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Walk-forward analysis
# ---------------------------------------------------------------------------
_OBJECTIVES = {
    "profit_factor": lambda m: (math.inf if m["profit_factor"] is None
                                else m["profit_factor"]),
    "total_return_pct": lambda m: m["total_return_pct"],
    "sharpe": lambda m: m["sharpe"],
}


def _slice_history(history: Dict[str, List[float]], lo: int,
                   hi: int) -> Dict[str, List[float]]:
    return {b: v[lo:hi] for b, v in history.items()}


def walk_forward(config: Config, history: Dict[str, List[float]],
                 folds: int = 4, objective: str = "profit_factor",
                 fee_rate: float | None = None,
                 slippage_rate: float | None = None,
                 funding_rate: float | None = None,
                 funding_schedule: Dict[str, List[float]] | None = None) -> dict:
    """Rolling walk-forward: pick the best in-sample strategy, test out-of-sample.

    The history is split into ``folds + 1`` equal segments. For each fold the
    engine ranks every strategy on segment *i* (in-sample), then trades the
    winner — unseen — on segment *i+1* (out-of-sample). Out-of-sample returns
    are compounded. This answers the only question that matters: does picking
    the strategy that looked best on the past actually work on the future?
    """
    if objective not in _OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(_OBJECTIVES)}")
    length = min(len(v) for v in history.values())
    folds = max(1, folds)
    seg = length // (folds + 1)
    if seg < 100:
        raise ValueError(
            "not enough history for walk-forward — use more bars or fewer folds "
            f"(each of {folds + 1} segments would be only {seg} bars)"
        )

    score = _OBJECTIVES[objective]
    rows: List[dict] = []
    compound = 1.0
    for i in range(folds):
        train = _slice_history(history, i * seg, (i + 1) * seg)
        test = _slice_history(history, (i + 1) * seg, (i + 2) * seg)
        train_fund = (_slice_history(funding_schedule, i * seg, (i + 1) * seg)
                      if funding_schedule else None)
        test_fund = (_slice_history(funding_schedule, (i + 1) * seg, (i + 2) * seg)
                     if funding_schedule else None)
        ranked = compare_strategies(config, train, fee_rate=fee_rate,
                                    slippage_rate=slippage_rate,
                                    funding_rate=funding_rate,
                                    funding_schedule=train_fund)
        winner = max(ranked, key=score)
        cfg = copy.deepcopy(config)
        cfg.strategy.name = winner["strategy"]
        cfg.strategy.params = {}
        oos = run_backtest(cfg, test, fee_rate=fee_rate,
                           slippage_rate=slippage_rate, funding_rate=funding_rate,
                           funding_schedule=test_fund)
        compound *= 1 + oos["total_return_pct"] / 100
        rows.append({
            "fold": i + 1,
            "train_winner": winner["strategy"],
            "train_label": winner["label"],
            "train_score": score(winner),
            "test_return_pct": oos["total_return_pct"],
            "test_profit_factor": oos["profit_factor"],
            "test_max_drawdown_pct": oos["max_drawdown_pct"],
            "test_win_rate_pct": oos["win_rate_pct"],
            "test_trades": oos["trades"],
        })

    profitable = sum(1 for r in rows if r["test_return_pct"] > 0)
    avg = sum(r["test_return_pct"] for r in rows) / len(rows)
    return {
        "folds": rows,
        "summary": {
            "num_folds": folds,
            "objective": objective,
            "segment_bars": seg,
            "oos_compound_return_pct": (compound - 1) * 100,
            "avg_fold_return_pct": avg,
            "folds_profitable": profitable,
            "folds_total": folds,
        },
    }


def _downsample(values: List[float], target: int) -> List[float]:
    if len(values) <= target:
        return values
    step = len(values) / target
    return [values[int(i * step)] for i in range(target)]
