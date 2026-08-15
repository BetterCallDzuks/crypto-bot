"""Accounting tests for the multi-symbol margin/leverage portfolio."""

from kovanica.state import PortfolioState


def _state(bases=("BTC",)):
    bases = list(bases)
    symbols = {b: f"{b}/USDC:USDC" for b in bases}
    return PortfolioState(bases=bases, symbols=symbols, starting_balance=10_000,
                          quote_currency="USDC", futures=True, leverage=5)


def test_long_pnl_and_shared_balance():
    s = _state()
    s.set_price("BTC", 100)
    s.open_position("BTC", "long", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    assert s.quote_balance == 7_500                 # margin locked from shared
    s.set_price("BTC", 110)
    assert s.equity() == 10_000 + 1_250             # +10% * 125 units
    pnl = s.close_position("BTC", 110, reason="take_profit")
    assert pnl == 1_250
    assert s.quote_balance == 11_250


def test_short_pnl_is_inverted():
    s = _state()
    s.open_position("BTC", "short", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    s.set_price("BTC", 90)
    assert s.equity() == 10_000 + 1_250             # short profits as price falls
    pnl = s.close_position("BTC", 90, reason="take_profit")
    assert pnl == 1_250


def test_liquidation_loss_capped_at_margin():
    s = _state()
    s.open_position("BTC", "long", quantity=125, price=100, margin=2_500,
                    leverage=5, reason="signal")
    pnl = s.close_position("BTC", 70, reason="liquidation")
    assert pnl == -2_500                            # capped at posted margin
    assert s.quote_balance == 7_500


def test_daily_pnl_bucketed_and_per_symbol_realized():
    s = _state(("BTC", "ETH"))
    s.open_position("BTC", "long", 1, 100, margin=20, leverage=5, reason="sig")
    s.close_position("BTC", 110, reason="take_profit")   # +10
    s.open_position("ETH", "long", 1, 100, margin=20, leverage=5, reason="sig")
    s.close_position("ETH", 95, reason="stop_loss")      # -5
    snap = s.snapshot()
    assert len(snap["daily_pnl"]) == 1
    assert round(snap["daily_pnl"][0]["pnl"], 2) == 5.0  # 10 - 5, same day
    per = {x["base"]: x["realized_pnl"] for x in snap["symbols"]}
    assert per["BTC"] == 10 and per["ETH"] == -5


def test_snapshot_shape_multi_symbol():
    s = _state(("BTC", "ETH", "SOL"))
    for b, p in (("BTC", 65000), ("ETH", 3200), ("SOL", 150)):
        s.set_price(b, p)
    snap = s.snapshot()
    assert snap["num_symbols"] == 3
    assert snap["quote_currency"] == "USDC"
    assert {x["base"] for x in snap["symbols"]} == {"BTC", "ETH", "SOL"}
    assert snap["open_positions"] == 0
