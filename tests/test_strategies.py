"""Tests for the expanded strategy library and its indicators."""

import pytest

from cryptobot.strategy import (
    REGISTRY,
    BollingerStrategy,
    ConfluenceStrategy,
    DonchianBreakoutStrategy,
    Signal,
    available_strategies,
    build_strategy,
    ema_series,
    rsi_series,
)


# -- indicator helpers -----------------------------------------------------
def test_ema_series_flat_is_flat():
    assert ema_series([5, 5, 5, 5], 2) == [5, 5, 5]


def test_ema_series_needs_enough_data():
    assert ema_series([1], 2) == []


def test_rsi_all_gains_is_100():
    r = rsi_series([1, 2, 3, 4, 5], 2)
    assert r[-1] == 100.0


def test_rsi_all_losses_is_0():
    r = rsi_series([5, 4, 3, 2, 1], 2)
    assert r[-1] == 0.0


# -- deterministic strategies ---------------------------------------------
def test_donchian_breakout_up_and_down():
    strat = DonchianBreakoutStrategy(period=3)
    assert strat.evaluate([3, 3, 3, 10]) is Signal.BUY   # breaks prior high
    assert strat.evaluate([3, 3, 3, 1]) is Signal.SELL   # breaks prior low
    assert strat.evaluate([3, 3, 3, 3]) is Signal.HOLD


def test_bollinger_reversion_buy_and_sell():
    strat = BollingerStrategy(period=3, num_std=1.0)
    # Prev close pokes below the lower band, current re-enters -> BUY.
    assert strat.evaluate([10, 10, 10, 7, 10]) is Signal.BUY
    # Prev close poked above the upper band, current re-enters -> SELL.
    assert strat.evaluate([10, 10, 10, 13, 10]) is Signal.SELL


# -- behavioral (trend reversal) ------------------------------------------
# A V-shape and an inverted-V, long enough for windows to span the reversal.
DOWN_THEN_UP = [120 - i for i in range(60)] + [61 + i for i in range(60)]
UP_THEN_DOWN = [60 + i for i in range(60)] + [119 - i for i in range(60)]


def _signals(strat, prices):
    """Evaluate over a fixed sliding window, exactly as the engine feeds data."""
    w = strat.warmup + 5
    return [strat.evaluate(prices[end - w:end])
            for end in range(w, len(prices) + 1)]


@pytest.mark.parametrize("name", ["ema_crossover", "sma_crossover", "rsi"])
def test_reversal_produces_buy_then_sell(name):
    strat = build_strategy(name)
    assert Signal.BUY in _signals(strat, DOWN_THEN_UP)
    assert Signal.SELL in _signals(strat, UP_THEN_DOWN)


def test_macd_crosses_on_momentum_shift():
    # A flat baseline then a sharp move makes the MACD line cross its signal.
    macd = build_strategy("macd")
    assert macd.evaluate([100] * 40 + [130]) is Signal.BUY
    assert macd.evaluate([100] * 40 + [70]) is Signal.SELL


def test_confluence_requires_agreement_and_signals_on_reversal():
    strat = build_strategy("confluence", {"threshold": 1})
    assert Signal.BUY in _signals(strat, DOWN_THEN_UP)
    assert Signal.SELL in _signals(strat, UP_THEN_DOWN)


def test_confluence_threshold_validated():
    with pytest.raises(ValueError):
        ConfluenceStrategy(threshold=5)


# -- registry / factory ----------------------------------------------------
def test_all_registered_strategies_build_with_defaults():
    catalogue = available_strategies()
    assert set(catalogue) == set(REGISTRY)
    for name in catalogue:
        strat = build_strategy(name)
        assert hasattr(strat, "evaluate") and strat.warmup >= 1
        # Evaluates cleanly on a plain ramp without raising.
        assert strat.evaluate([100 + i for i in range(120)]) in (
            Signal.BUY, Signal.SELL, Signal.HOLD)


def test_build_unknown_strategy_raises():
    with pytest.raises(ValueError, match="Unknown strategy"):
        build_strategy("moon_math")


def test_params_override_defaults():
    strat = build_strategy("ema_crossover", {"fast_period": 5, "slow_period": 20})
    assert strat.fast_period == 5 and strat.slow_period == 20
