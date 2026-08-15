"""Tests for the backtesting engine."""

from cryptobot.backtest import (
    ReplayExchange,
    compare_strategies,
    generate_sim_history,
    run_backtest,
)
from cryptobot.config import Config, FuturesConfig, MarketConfig, StrategyConfig


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
    from cryptobot.strategy import REGISTRY
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
    from cryptobot.state import PortfolioState
    s = PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                       starting_balance=10_000, futures=True, leverage=5)
    s.open_position("BTC", "long", 1, 100, margin=20, leverage=5, reason="x")
    assert s.symbols["BTC"].position.entry_price == 100      # no slippage
    pnl = s.close_position("BTC", 110, reason="tp")
    assert pnl == 10 and s.total_fees == 0.0                 # no fees
