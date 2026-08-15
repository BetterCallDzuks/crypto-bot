"""Smoke test for the research report generator."""

import datetime as dt

from cryptobot.backtest import compare_strategies, generate_sim_history, walk_forward
from cryptobot.config import Config, FuturesConfig, MarketConfig, StrategyConfig
from cryptobot.research import generate, render


def _config():
    return Config(
        market=MarketConfig(source="simulated", quote_currency="USDC",
                            symbols=["BTC", "ETH"]),
        strategy=StrategyConfig(name="sma_crossover",
                                params={"fast_period": 3, "slow_period": 8}),
        futures=FuturesConfig(enabled=True, leverage=3),
    )


def test_render_produces_markdown_report():
    cfg = _config()
    hist = generate_sim_history(cfg, bars=2000, seed=2)
    wf = walk_forward(cfg, hist, folds=3, objective="profit_factor")
    ranking = compare_strategies(cfg, hist)
    report = render(cfg, "simulated", dt.datetime(2026, 1, 1), wf, ranking,
                    historical_funding=False)
    assert "# Walk-forward research report" in report
    assert "Out-of-sample" in report
    assert "OOS compounded return" in report
    assert "Full-period ranking" in report
    for label in ("Confluence (ensemble)", "RSI reversion", "Donchian breakout"):
        assert label in report


def test_generate_writes_report(tmp_path):
    cfg = _config()
    result = generate(cfg, bars=1600, folds=3, source="simulated",
                      out_dir=str(tmp_path))
    assert result["path"] is not None
    assert (tmp_path / "latest.md").exists()
    assert "OOS compounded return" in result["report"]
    assert "oos_compound_return_pct" in result["summary"]
