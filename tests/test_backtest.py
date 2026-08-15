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
