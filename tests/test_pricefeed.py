"""Tests for the live price feed parsing and the engine's risk monitor."""

import json

from kovanica.pricefeed import LivePriceFeed, stream_symbol


def _feed():
    return LivePriceFeed(["BTC", "ETH"], "USDC")


def test_stream_symbol():
    assert stream_symbol("BTC", "USDC") == "btcusdc"


def test_url_uses_markprice_streams():
    url = _feed().url()
    assert "btcusdc@markPrice@1s" in url and "ethusdc@markPrice@1s" in url
    assert url.startswith("wss://fstream.binance.com/stream?streams=")


def test_testnet_url_when_sandbox():
    feed = LivePriceFeed(["BTC"], "USDC", sandbox=True)
    assert feed.url().startswith("wss://stream.binancefuture.com/stream")


def test_parse_combined_stream_message():
    feed = _feed()
    msg = json.dumps({"stream": "btcusdc@markPrice@1s",
                      "data": {"e": "markPriceUpdate", "s": "BTCUSDC", "p": "65000.5"}})
    assert feed.parse_message(msg) == {"BTC": 65000.5}


def test_parse_raw_message_and_apply_updates_price():
    feed = _feed()
    feed._apply(json.dumps({"s": "ETHUSDC", "p": "3200.25"}))
    assert feed.get("ETH") == 3200.25
    assert feed.get("BTC") is None


def test_parse_ignores_unknown_or_malformed():
    feed = _feed()
    assert feed.parse_message("not json") == {}
    assert feed.parse_message(json.dumps({"data": {"s": "DOGEUSDC", "p": "1"}})) == {}
    assert feed.parse_message(json.dumps({"data": {"s": "BTCUSDC"}})) == {}


# -- engine risk monitor with a fake feed ----------------------------------
class FakeFeed:
    def __init__(self):
        self.connected = True
        self._p = {}
        self.started = False
    def set(self, base, price):
        self._p[base] = price
    def get(self, base):
        return self._p.get(base)
    def start(self):
        self.started = True
    def stop(self):
        self.connected = False


def test_risk_monitor_closes_position_on_live_stop_loss():
    from kovanica.config import (
        Config, FuturesConfig, MarketConfig, RiskConfig, StrategyConfig, TradingConfig,
    )
    from kovanica.state import PortfolioState
    from kovanica.trader import TradingEngine

    cfg = Config(
        market=MarketConfig(source="exchange", quote_currency="USDC", symbols=["BTC"]),
        trading=TradingConfig(dry_run=True),
        strategy=StrategyConfig(name="sma_crossover",
                                params={"fast_period": 2, "slow_period": 3}),
        risk=RiskConfig(position_size_pct=0.15, stop_loss_pct=0.02,
                        take_profit_pct=0.04, max_daily_loss_pct=0.9),
        futures=FuturesConfig(enabled=True, leverage=5),
    )
    state = PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                           starting_balance=10_000, futures=True, leverage=5)

    class NoOpExchange:
        def fetch_closes(self, base, limit): return []
        def create_order(self, *a, **k): return {"simulated": True}

    feed = FakeFeed()
    engine = TradingEngine(cfg, NoOpExchange(), state, price_feed=feed)

    # Open a long at 100, then let the live feed drop below the 2% stop.
    state.set_price("BTC", 100)
    state.open_position("BTC", "long", 1, 100, margin=20, leverage=5, reason="x")
    feed.set("BTC", 97)                          # 3% down, past the 2% stop

    # Drive one monitor pass directly (deterministic, no threads).
    _, limits, _, _ = engine._settings()
    price = feed.get("BTC")
    state.set_price("BTC", price)
    engine._check_exits("BTC", state.symbols["BTC"].position, price, limits)

    assert state.symbols["BTC"].position is None
    assert state.trades[0].reason == "stop_loss"
