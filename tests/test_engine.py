"""End-to-end engine test using a fake exchange (no network, dry-run).

Verifies the full decision path: strategy signal -> risk sizing -> simulated
fill -> state update, plus the stop-loss exit.
"""

from cryptobot.config import (
    Config,
    MarketConfig,
    RiskConfig,
    StrategyConfig,
    TradingConfig,
)
from cryptobot.state import BotState
from cryptobot.trader import TradingEngine


class FakeExchange:
    """Feeds a scripted list of close-price series to the engine."""

    def __init__(self, series):
        self._series = list(series)
        self.orders = []

    def fetch_closes(self, limit):
        return self._series.pop(0)

    def create_market_buy(self, quantity, price):
        self.orders.append(("buy", quantity, price))
        return {"simulated": True}

    def create_market_sell(self, quantity, price):
        self.orders.append(("sell", quantity, price))
        return {"simulated": True}


def _config():
    return Config(
        market=MarketConfig(symbol="BTC/USDT", quote_currency="USDT"),
        trading=TradingConfig(dry_run=True, paper_starting_balance=10_000),
        strategy=StrategyConfig(name="sma_crossover", fast_period=2,
                                slow_period=3),
        risk=RiskConfig(position_size_pct=0.25, stop_loss_pct=0.02,
                        take_profit_pct=0.04, max_daily_loss_pct=0.05),
    )


def test_engine_buys_on_signal_then_stop_loss_exits():
    cfg = _config()
    state = BotState(starting_balance=10_000, dry_run=True)

    # Tick 1: upward crossover -> BUY near price 20.
    buy_series = [10, 9, 8, 8, 20]
    # Tick 2: price collapses well below the 2% stop -> stop-loss SELL.
    crash_series = [10, 9, 8, 8, 5]

    fake = FakeExchange([buy_series, crash_series])
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.position is not None, "engine should have opened a position"
    assert fake.orders[0][0] == "buy"
    entry = state.position.entry_price

    engine.tick()
    assert state.position is None, "stop-loss should have closed the position"
    assert fake.orders[-1][0] == "sell"
    # Realized a loss (sold below entry).
    assert state.realized_pnl < 0
    assert entry > state.trades[0].price  # last trade is the losing sell
