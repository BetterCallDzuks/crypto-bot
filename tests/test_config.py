import pytest

from kovanica.config import (
    Config,
    ExchangeConfig,
    FuturesConfig,
    MarketConfig,
    StrategyConfig,
    TradingConfig,
)


def test_defaults_validate():
    Config().validate()  # should not raise


def test_live_trading_requires_keys():
    cfg = Config(trading=TradingConfig(dry_run=False),
                 exchange=ExchangeConfig(api_key="", api_secret=""))
    with pytest.raises(ValueError, match="credentials"):
        cfg.validate()


def test_live_trading_with_keys_ok():
    cfg = Config(
        trading=TradingConfig(dry_run=False),
        market=MarketConfig(source="exchange"),
        exchange=ExchangeConfig(api_key="k", api_secret="s"),
    )
    cfg.validate()  # should not raise


def test_fast_must_be_below_slow():
    cfg = Config(strategy=StrategyConfig(
        name="sma_crossover", params={"fast_period": 21, "slow_period": 9}))
    with pytest.raises(ValueError):
        cfg.validate()


def test_unknown_strategy_rejected():
    cfg = Config(strategy=StrategyConfig(name="crystal_ball"))
    with pytest.raises(ValueError, match="Unknown strategy"):
        cfg.validate()


def test_strategy_update_and_param_coercion():
    cfg = Config()
    cfg.apply_updates({"strategy": {"name": "rsi",
                                    "params": {"period": "10",
                                               "oversold": "25",
                                               "overbought": "75"}}})
    assert cfg.strategy.name == "rsi"
    assert cfg.strategy.params == {"period": 10, "oversold": 25, "overbought": 75}


def test_invalid_strategy_params_roll_back():
    cfg = Config()
    before = (cfg.strategy.name, dict(cfg.strategy.params))
    with pytest.raises(ValueError):
        # oversold >= overbought is invalid
        cfg.apply_updates({"strategy": {"name": "rsi",
                                        "params": {"oversold": "80",
                                                   "overbought": "70"}}})
    assert (cfg.strategy.name, cfg.strategy.params) == before


def test_simulated_source_cannot_be_live():
    cfg = Config(market=MarketConfig(source="simulated"),
                 trading=TradingConfig(dry_run=False),
                 exchange=ExchangeConfig(api_key="k", api_secret="s"))
    with pytest.raises(ValueError, match="simulated"):
        cfg.validate()


def test_unknown_source_rejected():
    cfg = Config(market=MarketConfig(source="carrier-pigeon"))
    with pytest.raises(ValueError):
        cfg.validate()


def test_risk_pct_out_of_range_rejected():
    from kovanica.config import RiskConfig
    cfg = Config(risk=RiskConfig(position_size_pct=1.5))
    with pytest.raises(ValueError):
        cfg.validate()


def test_futures_leverage_must_be_at_least_one():
    cfg = Config(futures=FuturesConfig(enabled=True, leverage=0))
    with pytest.raises(ValueError, match="leverage"):
        cfg.validate()


def test_futures_margin_mode_validated():
    cfg = Config(futures=FuturesConfig(enabled=True, margin_mode="hyper"))
    with pytest.raises(ValueError, match="margin_mode"):
        cfg.validate()


def test_futures_defaults_validate():
    Config(futures=FuturesConfig(enabled=True, leverage=5,
                                 margin_mode="isolated")).validate()


def test_market_symbol_derivation_futures_and_spot():
    cfg = Config(market=MarketConfig(quote_currency="USDC"),
                 futures=FuturesConfig(enabled=True))
    assert cfg.market_symbol("BTC") == "BTC/USDC:USDC"
    cfg.futures.enabled = False
    assert cfg.market_symbol("BTC") == "BTC/USDC"
    # A full symbol passes through untouched.
    assert cfg.market_symbol("ETH/BNFCR:BNFCR") == "ETH/BNFCR:BNFCR"


def test_empty_symbols_rejected():
    cfg = Config(market=MarketConfig(symbols=[]))
    with pytest.raises(ValueError, match="symbols"):
        cfg.validate()


def test_apply_updates_coerces_and_applies():
    # Provide keys so flipping dry_run off passes the live-trading guard.
    cfg = Config(exchange=ExchangeConfig(api_key="k", api_secret="s"))
    cfg.apply_updates({
        "futures": {"leverage": "3"},                 # str -> int
        "risk": {"stop_loss_pct": "0.03"},            # str -> float
        "trading": {"dry_run": "false"},              # str -> bool
        "market": {"symbols": "btc, eth ,sol"},       # str -> upper list
    })
    assert cfg.futures.leverage == 3
    assert cfg.risk.stop_loss_pct == 0.03
    assert cfg.trading.dry_run is False
    assert cfg.market.symbols == ["BTC", "ETH", "SOL"]


def test_apply_updates_rolls_back_on_invalid():
    cfg = Config()
    original = cfg.risk.stop_loss_pct
    with pytest.raises(ValueError):
        cfg.apply_updates({"risk": {"stop_loss_pct": "5"}})  # out of (0,1]
    assert cfg.risk.stop_loss_pct == original             # unchanged


def test_save_and_reload_round_trip(tmp_path):
    from kovanica.config import load_config, save_config
    cfg = Config()
    cfg.apply_updates({"futures": {"leverage": 7},
                       "market": {"symbols": ["BTC", "ETH"]}})
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    text = path.read_text()
    assert "api_key" not in text                          # secrets never saved
    reloaded = load_config(path, env_path=tmp_path / ".env")
    assert reloaded.futures.leverage == 7
    assert reloaded.market.symbols == ["BTC", "ETH"]


def test_local_overlay_overrides_base_without_touching_it(tmp_path):
    import yaml as _yaml

    from kovanica.config import load_config
    base = tmp_path / "config.yaml"
    base.write_text(_yaml.safe_dump({
        "futures": {"leverage": 5},
        "web": {"timezone": "UTC", "port": 4000},
    }))
    base_before = base.read_text()
    # The overlay changes only some fields; the base file is never rewritten.
    (tmp_path / "config.local.yaml").write_text(_yaml.safe_dump({
        "futures": {"leverage": 3},
        "web": {"timezone": "Europe/Zagreb"},
    }))
    cfg = load_config(base, env_path=tmp_path / ".env")
    assert cfg.futures.leverage == 3                      # overlay wins
    assert cfg.web.timezone == "Europe/Zagreb"
    assert cfg.web.port == 4000                           # base value preserved
    assert base.read_text() == base_before               # base untouched
