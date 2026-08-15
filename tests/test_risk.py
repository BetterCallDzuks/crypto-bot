from kovanica.risk import (
    RiskLimits,
    daily_loss_limit_hit,
    margin_to_use,
    position_quantity,
    should_liquidate,
    should_stop_loss,
    should_take_profit,
)

LIMITS = RiskLimits(
    position_size_pct=0.25,
    stop_loss_pct=0.02,
    take_profit_pct=0.04,
    max_daily_loss_pct=0.05,
)


def test_margin_is_fraction_of_balance():
    assert margin_to_use(10_000, LIMITS) == 2_500
    assert margin_to_use(0, LIMITS) == 0.0


def test_position_quantity_scales_with_leverage():
    # margin 2_500 * 1x / price 100 = 25 units.
    assert position_quantity(10_000, 100, 1, LIMITS) == 25.0
    # 5x leverage => notional 12_500 / 100 = 125 units.
    assert position_quantity(10_000, 100, 5, LIMITS) == 125.0


def test_position_quantity_guards_bad_inputs():
    assert position_quantity(0, 100, 5, LIMITS) == 0.0
    assert position_quantity(10_000, 0, 5, LIMITS) == 0.0
    assert position_quantity(10_000, 100, 0, LIMITS) == 0.0


def test_stop_loss_is_side_aware():
    # Long: stop 2% below entry.
    assert should_stop_loss("long", 100, 98, LIMITS) is True
    assert should_stop_loss("long", 100, 98.01, LIMITS) is False
    # Short: stop 2% above entry (price rising hurts a short).
    assert should_stop_loss("short", 100, 102, LIMITS) is True
    assert should_stop_loss("short", 100, 101.99, LIMITS) is False


def test_take_profit_is_side_aware():
    # Long: target 4% above.
    assert should_take_profit("long", 100, 104, LIMITS) is True
    assert should_take_profit("long", 100, 103.99, LIMITS) is False
    # Short: target 4% below.
    assert should_take_profit("short", 100, 96, LIMITS) is True
    assert should_take_profit("short", 100, 96.01, LIMITS) is False


def test_liquidation_price_by_leverage():
    # 5x long liquidates ~20% below entry (100 -> 80).
    assert should_liquidate("long", 100, 80, 5) is True
    assert should_liquidate("long", 100, 80.5, 5) is False
    # 5x short liquidates ~20% above entry (100 -> 120).
    assert should_liquidate("short", 100, 120, 5) is True
    assert should_liquidate("short", 100, 119.5, 5) is False


def test_daily_loss_limit():
    # 5% of 10_000 = 500 max loss. Equity 9_500 => hit exactly.
    assert daily_loss_limit_hit(10_000, 9_500, LIMITS) is True
    assert daily_loss_limit_hit(10_000, 9_501, LIMITS) is False
    assert daily_loss_limit_hit(10_000, 10_000, LIMITS) is False
