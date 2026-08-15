"""Configuration loading and validation.

Config comes from two places:
  * ``config.yaml`` — non-secret settings (exchange, strategy, risk limits).
  * ``.env``        — secrets (API keys), loaded into the environment.

Splitting them keeps credentials out of version control while allowing the
rest of the configuration to be committed and reviewed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class ExchangeConfig:
    id: str = "binance"
    sandbox: bool = True
    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""


@dataclass
class MarketConfig:
    symbol: str = "BTC/USDT"
    timeframe: str = "1m"
    quote_currency: str = "USDT"
    # "exchange" = live ccxt market data; "simulated" = offline random walk.
    source: str = "exchange"


@dataclass
class TradingConfig:
    dry_run: bool = True
    poll_interval: int = 30
    paper_starting_balance: float = 10_000.0


@dataclass
class StrategyConfig:
    name: str = "sma_crossover"
    fast_period: int = 9
    slow_period: int = 21


@dataclass
class RiskConfig:
    position_size_pct: float = 0.25
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    max_daily_loss_pct: float = 0.05


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 5000


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    web: WebConfig = field(default_factory=WebConfig)

    def validate(self) -> None:
        """Fail fast on nonsensical or unsafe configuration."""
        s = self.strategy
        if s.fast_period < 1 or s.slow_period < 1:
            raise ValueError("strategy periods must be >= 1")
        if s.fast_period >= s.slow_period:
            raise ValueError(
                "strategy.fast_period must be smaller than strategy.slow_period"
            )

        r = self.risk
        for name in ("position_size_pct", "stop_loss_pct",
                     "take_profit_pct", "max_daily_loss_pct"):
            value = getattr(r, name)
            if not 0 < value <= 1:
                raise ValueError(f"risk.{name} must be within (0, 1], got {value}")

        if self.trading.poll_interval < 1:
            raise ValueError("trading.poll_interval must be >= 1 second")

        if self.market.source not in ("exchange", "simulated"):
            raise ValueError(
                "market.source must be 'exchange' or 'simulated', got "
                f"'{self.market.source}'"
            )

        # A simulated market can never place real orders, so live trading
        # against it is a configuration mistake — catch it early.
        if self.market.source == "simulated" and not self.trading.dry_run:
            raise ValueError(
                "market.source: simulated cannot be used with "
                "trading.dry_run: false (there is no real exchange to trade on)."
            )

        # The critical live-trading guard: real orders require real keys.
        if not self.trading.dry_run and not (
            self.exchange.api_key and self.exchange.api_secret
        ):
            raise ValueError(
                "Live trading is enabled (trading.dry_run = false) but no API "
                "credentials were found. Set EXCHANGE_API_KEY and "
                "EXCHANGE_API_SECRET in your .env file, or set dry_run: true."
            )


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config section '{key}' must be a mapping")
    return value


def load_config(
    config_path: str | os.PathLike[str] = "config.yaml",
    env_path: str | os.PathLike[str] = ".env",
) -> Config:
    """Load configuration from YAML + environment, validate, and return it."""
    # Load secrets into os.environ first (silently no-ops if .env is absent).
    load_dotenv(env_path)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy the provided config.yaml."
        )

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a mapping at the top level")

    exchange = ExchangeConfig(
        **_section(raw, "exchange"),
        api_key=os.environ.get("EXCHANGE_API_KEY", ""),
        api_secret=os.environ.get("EXCHANGE_API_SECRET", ""),
        api_password=os.environ.get("EXCHANGE_API_PASSWORD", ""),
    )

    cfg = Config(
        exchange=exchange,
        market=MarketConfig(**_section(raw, "market")),
        trading=TradingConfig(**_section(raw, "trading")),
        strategy=StrategyConfig(**_section(raw, "strategy")),
        risk=RiskConfig(**_section(raw, "risk")),
        web=WebConfig(**_section(raw, "web")),
    )
    cfg.validate()
    return cfg
