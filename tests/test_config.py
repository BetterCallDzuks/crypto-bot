import pytest

from cryptobot.config import (
    Config,
    ExchangeConfig,
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
    cfg = Config(strategy=StrategyConfig(fast_period=21, slow_period=9))
    with pytest.raises(ValueError):
        cfg.validate()


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
    from cryptobot.config import RiskConfig
    cfg = Config(risk=RiskConfig(position_size_pct=1.5))
    with pytest.raises(ValueError):
        cfg.validate()
