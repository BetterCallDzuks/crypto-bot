"""Accounting tests for the margin/leverage state model."""

from cryptobot.state import BotState


def _state():
    return BotState(starting_balance=10_000, futures=True, leverage=5)


def test_long_pnl_and_equity():
    s = _state()
    # Open a 5x long: margin 2_500, notional 12_500, qty 125 @ 100.
    s.open_position("long", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    assert s.quote_balance == 7_500                 # margin locked out
    # Price +10% -> pnl = (110-100)*125 = 1_250.
    assert s.equity(110) == 10_000 + 1_250
    pnl = s.close_position(110, reason="take_profit")
    assert pnl == 1_250
    assert s.quote_balance == 11_250                # margin returned + profit


def test_short_pnl_is_inverted():
    s = _state()
    s.open_position("short", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    # Price falls 10% -> a short profits: (100-90)*125 = 1_250.
    assert s.equity(90) == 10_000 + 1_250
    # Price rises 10% -> a short loses.
    assert s.equity(110) == 10_000 - 1_250
    pnl = s.close_position(90, reason="take_profit")
    assert pnl == 1_250


def test_liquidation_loss_capped_at_margin():
    s = _state()
    s.open_position("long", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    pnl = s.close_position(70, reason="liquidation")   # brutal move
    # Even though raw pnl would be (70-100)*125 = -3_750, liquidation caps
    # the loss at the posted margin.
    assert pnl == -2_500
    assert s.quote_balance == 7_500                     # margin gone, no more


def test_liquidation_price_helper():
    s = _state()
    s.open_position("long", quantity=1, price=100, margin=20, leverage=5,
                    reason="signal")
    assert s.position.liquidation_price() == 80         # 100 * (1 - 1/5)
