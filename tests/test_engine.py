"""End-to-end engine tests using a fake multi-symbol exchange (no network).

Covers the futures decision path per asset: open long, flip long<->short,
leveraged stop-loss and liquidation, long-only mode, and independent handling
of two symbols in one tick.
"""

from kovanica.config import (
    Config,
    FuturesConfig,
    MarketConfig,
    RiskConfig,
    StrategyConfig,
    TradingConfig,
)
from kovanica.state import PortfolioState
from kovanica.trader import TradingEngine

# Crossover series for fast=2 / slow=3.
BUY = [10, 9, 8, 8, 12]      # upward crossover -> BUY, last price 12
SELL = [12, 13, 14, 14, 10]  # downward crossover -> SELL, last price 10
FLAT = [10, 10, 10, 10, 10]  # no crossover -> HOLD


class FakeExchange:
    """Serves scripted per-base close series and records orders."""

    def __init__(self, scripts):
        # scripts: {base: [series_for_tick1, series_for_tick2, ...]}
        self._scripts = {b: list(v) for b, v in scripts.items()}
        self.orders = []

    def fetch_closes(self, base, limit):
        seq = self._scripts[base]
        return seq.pop(0) if seq else FLAT

    def create_order(self, base, side, quantity, price, reduce_only=False):
        self.orders.append((base, side, quantity, price, reduce_only))
        return {"simulated": True}


def _config(bases, leverage=5, allow_short=True, stop=0.02, tp=0.04):
    return Config(
        market=MarketConfig(source="simulated", quote_currency="USDC",
                            symbols=list(bases)),
        trading=TradingConfig(dry_run=True, paper_starting_balance=10_000),
        strategy=StrategyConfig(name="sma_crossover",
                                params={"fast_period": 2, "slow_period": 3}),
        risk=RiskConfig(position_size_pct=0.15, stop_loss_pct=stop,
                        take_profit_pct=tp, max_daily_loss_pct=0.9),
        futures=FuturesConfig(enabled=True, leverage=leverage,
                              margin_mode="isolated", allow_short=allow_short),
    )


def _state(bases, leverage=5):
    bases = list(bases)
    return PortfolioState(bases=bases,
                          symbols={b: f"{b}/USDC:USDC" for b in bases},
                          starting_balance=10_000, futures=True,
                          leverage=leverage)


def test_open_long_then_flip_short_then_back():
    cfg = _config(["BTC"], leverage=1, stop=0.9, tp=0.9)  # loose exits
    state = _state(["BTC"], leverage=1)
    fake = FakeExchange({"BTC": [BUY, SELL, BUY]})
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.symbols["BTC"].position.side == "long"
    engine.tick()
    assert state.symbols["BTC"].position.side == "short"
    engine.tick()
    assert state.symbols["BTC"].position.side == "long"

    sides = [o[1] for o in fake.orders]
    assert sides == ["buy", "sell", "sell", "buy", "buy"]
    reduce_flags = [o[4] for o in fake.orders]
    assert reduce_flags == [False, True, False, True, False]


def test_leveraged_stop_loss_closes_long():
    cfg = _config(["BTC"], leverage=5, stop=0.02)
    state = _state(["BTC"])
    drop = [12, 12, 12, 12, 11]   # >2% below the ~12 entry
    fake = FakeExchange({"BTC": [BUY, drop]})
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.symbols["BTC"].position.side == "long"
    engine.tick()
    assert state.symbols["BTC"].position is None
    assert state.trades[0].reason == "stop_loss"
    assert state.realized_pnl < 0


def test_liquidation_caps_loss_at_margin():
    cfg = _config(["BTC"], leverage=5, stop=0.9, tp=0.9)
    state = _state(["BTC"])
    crash = [12, 12, 12, 12, 8]   # ~33% drop, past 5x liquidation
    fake = FakeExchange({"BTC": [BUY, crash]})
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    margin = state.symbols["BTC"].position.margin
    engine.tick()
    assert state.symbols["BTC"].position is None
    assert state.trades[0].reason == "liquidation"
    assert state.realized_pnl == -margin


def test_long_only_mode_does_not_short():
    cfg = _config(["BTC"], allow_short=False, stop=0.9, tp=0.9)
    state = _state(["BTC"])
    fake = FakeExchange({"BTC": [BUY, SELL]})
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.symbols["BTC"].position.side == "long"
    engine.tick()
    assert state.symbols["BTC"].position is None      # closed, not shorted


def test_two_symbols_trade_independently():
    cfg = _config(["BTC", "ETH"], leverage=1, stop=0.9, tp=0.9)
    state = _state(["BTC", "ETH"], leverage=1)
    # BTC gets a BUY, ETH gets a SELL, in the same tick.
    fake = FakeExchange({"BTC": [BUY], "ETH": [SELL]})
    engine = TradingEngine(cfg, fake, state)

    engine.tick()
    assert state.symbols["BTC"].position.side == "long"
    assert state.symbols["ETH"].position.side == "short"
    # Each opened exactly one position; shared balance funded both margins.
    assert state.quote_balance < 10_000


def test_runtime_settings_reload_changes_leverage():
    cfg = _config(["BTC"], leverage=5)
    state = _state(["BTC"])
    engine = TradingEngine(cfg, fake_noop(), state)
    assert engine.leverage == 5
    cfg.apply_updates({"futures": {"leverage": 3}})
    engine.reload_from_config()
    assert engine.leverage == 3


def fake_noop():
    return FakeExchange({"BTC": []})
