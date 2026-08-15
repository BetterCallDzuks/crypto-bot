"""Tests for the backtesting engine."""

import pytest

from kovanica.backtest import (
    ReplayExchange,
    align_funding,
    compare_strategies,
    generate_sim_history,
    run_backtest,
    walk_forward,
)
from kovanica.config import Config, FuturesConfig, MarketConfig, StrategyConfig


def _config(strategy="sma_crossover"):
    return Config(
        market=MarketConfig(source="simulated", quote_currency="USDC",
                            symbols=["BTC", "ETH"]),
        strategy=StrategyConfig(name=strategy,
                                params={"fast_period": 3, "slow_period": 8}
                                if "sma" in strategy or "ema" in strategy else {}),
        futures=FuturesConfig(enabled=True, leverage=3),
    )


def test_generate_sim_history_shape():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=500, seed=1)
    assert set(hist) == {"BTC", "ETH"}
    assert all(len(v) == 500 for v in hist.values())


def test_replay_exchange_windows():
    ex = ReplayExchange({"BTC": [1, 2, 3, 4, 5]})
    ex.set_index(3)
    assert ex.fetch_closes("BTC", 2) == [2, 3]
    assert ex.fetch_price("BTC") == 3


def test_run_backtest_metrics_present_and_deterministic():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=800, seed=7)
    a = run_backtest(cfg, hist)
    b = run_backtest(cfg, hist)
    for key in ("total_return_pct", "max_drawdown_pct", "win_rate_pct",
                "profit_factor", "trades", "sharpe", "final_equity",
                "equity_curve", "bars"):
        assert key in a
    # Same inputs -> identical results (no hidden randomness in replay).
    assert a["final_equity"] == b["final_equity"]
    assert a["trades"] == b["trades"]
    assert a["bars"] == 800
    assert a["max_drawdown_pct"] <= 0            # drawdown is non-positive
    assert len(a["equity_curve"]) > 0


def test_compare_returns_all_strategies_sorted():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=800, seed=3)
    rows = compare_strategies(cfg, hist)
    from kovanica.strategy import REGISTRY
    assert {r["strategy"] for r in rows} == set(REGISTRY)
    returns = [r["total_return_pct"] for r in rows]
    assert returns == sorted(returns, reverse=True)   # ranked best-first


def test_backtest_does_not_mutate_input_config():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=400, seed=2)
    run_backtest(cfg, hist)
    # run_backtest works on a copy: the daily-loss limit is untouched here.
    assert cfg.risk.max_daily_loss_pct != 1.0


def test_costs_reduce_returns_and_are_reported():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=1000, seed=9)
    free = run_backtest(cfg, hist, fee_rate=0.0, slippage_rate=0.0)
    costed = run_backtest(cfg, hist, fee_rate=0.0004, slippage_rate=0.0005)
    # Same trades, but costs drag the result down and are tallied.
    assert free["fees_paid"] == 0.0
    assert costed["fees_paid"] > 0.0
    assert costed["final_equity"] < free["final_equity"]
    assert costed["total_return_pct"] < free["total_return_pct"]


def test_cost_model_defaults_come_from_config():
    cfg = _config()
    cfg.backtest.fee_rate = 0.001
    cfg.backtest.slippage_rate = 0.001
    hist = generate_sim_history(cfg, bars=600, seed=4)
    m = run_backtest(cfg, hist)          # no explicit rates -> use config
    assert m["fee_rate"] == 0.001 and m["slippage_rate"] == 0.001


def test_state_zero_cost_matches_plain_accounting():
    from kovanica.state import PortfolioState
    s = PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                       starting_balance=10_000, futures=True, leverage=5)
    s.open_position("BTC", "long", 1, 100, margin=20, leverage=5, reason="x")
    assert s.symbols["BTC"].position.entry_price == 100      # no slippage
    pnl = s.close_position("BTC", 110, reason="tp")
    assert pnl == 10 and s.total_fees == 0.0                 # no fees


def test_funding_charges_long_and_pays_short():
    from kovanica.state import PortfolioState
    # Long pays funding when the rate is positive.
    s = PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                       starting_balance=10_000, futures=True, leverage=5)
    s.open_position("BTC", "long", 2, 100, margin=40, leverage=5, reason="x")
    s.set_price("BTC", 100)
    s.apply_funding(0.001)                 # notional 200 * 0.001 = 0.20 paid
    assert round(s.total_funding, 6) == 0.20
    assert round(s.quote_balance, 6) == round(10_000 - 40 - 0.20, 6)

    # Short receives funding when the rate is positive.
    s2 = PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                        starting_balance=10_000, futures=True, leverage=5)
    s2.open_position("BTC", "short", 2, 100, margin=40, leverage=5, reason="x")
    s2.set_price("BTC", 100)
    s2.apply_funding(0.001)
    assert round(s2.total_funding, 6) == -0.20     # received


def test_funding_reported_in_metrics():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=1000, seed=5)
    m = run_backtest(cfg, hist, funding_rate=0.001)
    assert "funding_paid" in m and m["funding_rate"] == 0.001


def test_walk_forward_structure_and_folds():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=3000, seed=8)
    wf = walk_forward(cfg, hist, folds=4, objective="profit_factor")
    assert len(wf["folds"]) == 4
    assert wf["summary"]["num_folds"] == 4
    for r in wf["folds"]:
        assert r["train_winner"] in {
            "confluence", "ema_crossover", "sma_crossover", "macd",
            "rsi", "bollinger", "donchian"}
        assert "test_return_pct" in r
    assert 0 <= wf["summary"]["folds_profitable"] <= 4


def test_walk_forward_rejects_too_little_history():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=300, seed=1)
    with pytest.raises(ValueError, match="not enough history"):
        walk_forward(cfg, hist, folds=8)


def test_walk_forward_bad_objective():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=1500, seed=1)
    with pytest.raises(ValueError, match="objective"):
        walk_forward(cfg, hist, folds=2, objective="vibes")


# -- real funding schedule --------------------------------------------------
def test_align_funding_attaches_events_to_bars():
    # Events land on the first bar at/after their timestamp.
    ts = [0, 60, 120, 180]
    events = [(60, 0.001), (180, 0.002)]
    assert align_funding(ts, events) == [0.0, 0.001, 0.0, 0.002]


def test_align_funding_before_first_bar_and_empty():
    assert align_funding([100, 200], [(50, 0.001)]) == [0.001, 0.0]
    assert align_funding([100, 200], []) == [0.0, 0.0]
    assert align_funding([], [(1, 0.001)]) == []


def test_align_funding_sums_multiple_in_one_bar():
    # Two events between bar 0 and bar 1 both attach to bar 1.
    ts = [0, 100]
    events = [(30, 0.001), (70, 0.002)]
    assert align_funding(ts, events) == [0.0, pytest.approx(0.003)]


def test_run_backtest_uses_funding_schedule_over_flat():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=1000, seed=5)
    length = min(len(v) for v in hist.values())
    zero = {b: [0.0] * length for b in hist}
    big = {b: [0.002] * length for b in hist}     # extreme, to force an effect
    z = run_backtest(cfg, hist, funding_schedule=zero)
    b = run_backtest(cfg, hist, funding_schedule=big)
    assert z["funding_source"] == "historical"
    assert z["funding_paid"] == 0.0
    assert abs(b["funding_paid"]) > 0.0           # positions were funded
