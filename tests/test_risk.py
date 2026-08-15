from cryptobot.risk import (
    RiskLimits,
    daily_loss_limit_hit,
    position_size,
    should_stop_loss,
    should_take_profit,
)

LIMITS = RiskLimits(
    position_size_pct=0.25,
    stop_loss_pct=0.02,
    take_profit_pct=0.04,
    max_daily_loss_pct=0.05,
)


def test_position_size_deploys_configured_fraction():
    # 25% of 10_000 = 2_500 spent at price 100 => 25 units.
    assert position_size(10_000, 100, LIMITS) == 25.0


def test_position_size_guards_bad_inputs():
    assert position_size(0, 100, LIMITS) == 0.0
    assert position_size(10_000, 0, LIMITS) == 0.0


def test_stop_loss_triggers_at_threshold():
    # 2% below entry of 100 is 98.
    assert should_stop_loss(100, 98, LIMITS) is True
    assert should_stop_loss(100, 98.01, LIMITS) is False


def test_take_profit_triggers_at_threshold():
    # 4% above entry of 100 is 104.
    assert should_take_profit(100, 104, LIMITS) is True
    assert should_take_profit(100, 103.99, LIMITS) is False


def test_daily_loss_limit():
    # 5% of 10_000 = 500 max loss. Equity 9_500 => hit exactly.
    assert daily_loss_limit_hit(10_000, 9_500, LIMITS) is True
    assert daily_loss_limit_hit(10_000, 9_501, LIMITS) is False
    assert daily_loss_limit_hit(10_000, 10_000, LIMITS) is False
