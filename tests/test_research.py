"""Smoke test for the research report generator."""

import argparse

import research
from cryptobot.backtest import compare_strategies, generate_sim_history, walk_forward
from cryptobot.config import Config, FuturesConfig, MarketConfig, StrategyConfig


def _config():
    return Config(
        market=MarketConfig(source="simulated", quote_currency="USDC",
                            symbols=["BTC", "ETH"]),
        strategy=StrategyConfig(name="sma_crossover",
                                params={"fast_period": 3, "slow_period": 8}),
        futures=FuturesConfig(enabled=True, leverage=3),
    )


def test_render_produces_markdown_report():
    import datetime as dt
    cfg = _config()
    hist = generate_sim_history(cfg, bars=2000, seed=2)
    wf = walk_forward(cfg, hist, folds=3, objective="profit_factor")
    ranking = compare_strategies(cfg, hist)
    args = argparse.Namespace(source="simulated", bars=2000, folds=3)
    report = research._render(cfg, args, dt.datetime(2026, 1, 1), wf, ranking,
                              historical_funding=False)
    assert "# Walk-forward research report" in report
    assert "Out-of-sample" in report
    assert "OOS compounded return" in report
    assert "Full-period ranking" in report
    # Every strategy appears in the ranking table.
    for label in ("Confluence (ensemble)", "RSI reversion", "Donchian breakout"):
        assert label in report
