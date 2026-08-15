"""End-to-end engine tests using a fake exchange (no network, dry-run).

Covers the futures decision path: open long, flip long<->short on crossovers,
and leveraged stop-loss / liquidation exits.
"""

from cryptobot.config import (
    Config,
    FuturesConfig,
    MarketConfig,
    RiskConfig,
    StrategyConfig,
    TradingConfig,
)
from cryptobot.state import BotState
from cryptobot.trader import TradingEngine

# Crossover series for fast=2 / slow=3.
BUY = [10, 9, 8, 8, 12]      # upward crossover -> BUY, last price 12
SELL = [12, 13, 14, 14, 10]  # downward crossover -> SELL, last price 10


class FakeExchange:
    """Feeds scripted close-price series and records orders."""

    def __init__(self, series):
        self._series = list(series)
        self.orders = []

    def fetch_closes(self, limit):
        return self._series.pop(0)

    def create_order(self, side, quantity, price, reduce_only=False):
        self.orders.append((side, quantity, price, reduce_only))
        return {"simulated": True}


def _config(leverage=5, allow_short=True, stop=0.02, tp=0.04):
    return Config(
        market=MarketConfig(symbol="BTC/USDT:USDT", quote_currency="USDT"),
        trading=TradingConfig(dry_run=True, paper_starting_balance=10_000),
        strategy=StrategyConfig(name="sma_crossover", fast_period=2,
                                slow_period=3),
        risk=RiskConfig(position_size_pct=0.25, stop_loss_pct=stop,
                        take_profit_pct=tp, max_daily_loss_pct=0.9),
        futures=FuturesConfig(enabled=True, leverage=leverage,
                              margin_mode="isolated", allow_short=allow_short),
    )


def _state(cfg):
    return BotState(starting_balance=10_000, dry_run=True, futures=True,
                    leverage=cfg.futures.leverage)


def test_open_long_then_flip_to_short_then_back():
    # Loose stops/leverage so exits never fire — isolate the flip logic.
    cfg = _config(leverage=1, stop=0.9, tp=0.9)
    state = _state(cfg)
    fake = FakeExchange([BUY, SELL, BUY])
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.position is not None and state.position.side == "long"

    engine.tick()
    assert state.position is not None and state.position.side == "short"

    engine.tick()
    assert state.position is not None and state.position.side == "long"

    # Order sides tell the whole story:
    #   open long (buy), close long (sell), open short (sell),
    #   close short (buy), open long (buy).
    sides = [o[0] for o in fake.orders]
    assert sides == ["buy", "sell", "sell", "buy", "buy"]
    # The two closing orders must be reduce-only.
    reduce_flags = [o[3] for o in fake.orders]
    assert reduce_flags == [False, True, False, True, False]


def test_leveraged_stop_loss_closes_long():
    cfg = _config(leverage=5, stop=0.02)
    state = _state(cfg)
    # Open long near 12, then price drops to 11 (>2% below 12) -> stop-loss.
    drop = [12, 12, 12, 12, 11]
    fake = FakeExchange([BUY, drop])
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.position.side == "long"
    engine.tick()
    assert state.position is None            # stop-loss closed it
    assert state.trades[0].reason == "stop_loss"
    assert state.realized_pnl < 0            # leveraged loss on the drop


def test_liquidation_caps_loss_at_margin():
    cfg = _config(leverage=5, stop=0.9, tp=0.9)  # disable stop so liq is tested
    state = _state(cfg)
    entry_margin_before = None
    # Open long ~12, then crash below the 5x liquidation level (~20% -> 9.6).
    crash = [12, 12, 12, 12, 8]
    fake = FakeExchange([BUY, crash])
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    pos = state.position
    entry_margin_before = pos.margin
    engine.tick()
    assert state.position is None
    assert state.trades[0].reason == "liquidation"
    # Liquidation loss is capped at the posted margin, never more.
    assert state.realized_pnl == -entry_margin_before


def test_long_only_mode_does_not_short():
    cfg = _config(allow_short=False, stop=0.9, tp=0.9)
    state = _state(cfg)
    fake = FakeExchange([BUY, SELL])
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.position.side == "long"
    engine.tick()
    # SELL closes the long but must NOT open a short.
    assert state.position is None
