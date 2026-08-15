from kovanica.strategy import Signal, SmaCrossoverStrategy, build_strategy, sma


def test_sma_basic():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2, 3], 3) == 2.0


def test_sma_insufficient_data_returns_none():
    assert sma([1, 2], 3) is None


def test_holds_during_warmup():
    strat = SmaCrossoverStrategy(fast_period=2, slow_period=3)
    assert strat.evaluate([10, 11]) is Signal.HOLD


def test_buy_on_upward_crossover():
    # Fast MA rises through slow MA as price turns up.
    strat = SmaCrossoverStrategy(fast_period=2, slow_period=3)
    closes = [10, 9, 8, 8, 20]  # sharp upturn pushes fast above slow
    assert strat.evaluate(closes) is Signal.BUY


def test_sell_on_downward_crossover():
    strat = SmaCrossoverStrategy(fast_period=2, slow_period=3)
    closes = [10, 11, 12, 12, 2]  # sharp downturn pushes fast below slow
    assert strat.evaluate(closes) is Signal.SELL


def test_no_repeat_signal_when_no_crossover():
    strat = SmaCrossoverStrategy(fast_period=2, slow_period=3)
    # Steadily rising: fast stays above slow, so no *new* crossover.
    closes = [1, 2, 3, 4, 5, 6, 7]
    assert strat.evaluate(closes) is Signal.HOLD


def test_invalid_periods_rejected():
    import pytest
    with pytest.raises(ValueError):
        SmaCrossoverStrategy(fast_period=5, slow_period=5)


def test_factory_unknown_strategy():
    import pytest
    with pytest.raises(ValueError):
        build_strategy("does_not_exist", fast_period=1, slow_period=2)
