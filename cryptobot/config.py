"""Configuration loading, validation, runtime updates, and persistence.

Config comes from two places:
  * ``config.yaml`` — non-secret settings (markets, strategy, risk limits).
  * ``.env``        — secrets (API keys), loaded into the environment.

Splitting them keeps credentials out of version control. The settings page in
the dashboard edits the non-secret config at runtime and saves it back to
``config.yaml`` (secrets are never written).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Default basket of large-cap assets. Quote currency is applied separately.
DEFAULT_SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "DOGE", "BNB", "ADA"]


@dataclass
class ExchangeConfig:
    id: str = "binance"
    sandbox: bool = True
    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""


@dataclass
class MarketConfig:
    # "exchange" = live ccxt market data; "simulated" = offline random walk.
    source: str = "exchange"
    # Margin/quote asset. In Croatia/EEA, USDT is unavailable — use USDC or
    # BNFCR (Binance's EEA settlement asset).
    quote_currency: str = "USDC"
    timeframe: str = "1m"
    # Base assets to trade. The ccxt market symbol is derived from the quote
    # currency (e.g. BTC + USDC -> "BTC/USDC:USDC" for a perpetual).
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))


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
    position_size_pct: float = 0.15
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    max_daily_loss_pct: float = 0.05


@dataclass
class FuturesConfig:
    enabled: bool = True
    leverage: int = 5
    margin_mode: str = "isolated"   # "isolated" | "cross"
    allow_short: bool = True


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 4000
    # Require a login for the dashboard. Credentials come from .env, not YAML.
    auth_enabled: bool = True


@dataclass
class AuthConfig:
    """Dashboard login secrets, sourced from .env — never written to YAML."""
    username: str = "admin"
    password_hash: str = ""       # werkzeug hash of DASHBOARD_PASSWORD
    secret_key: str = ""          # Flask session signing key
    ephemeral_secret: bool = False  # true if secret_key was auto-generated

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    futures: FuturesConfig = field(default_factory=FuturesConfig)
    web: WebConfig = field(default_factory=WebConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)

    # -- auth helpers ------------------------------------------------------
    @property
    def auth_active(self) -> bool:
        """Login is enforced only when enabled AND a password is configured."""
        return self.web.auth_enabled and self.auth.has_password

    @property
    def host_is_local(self) -> bool:
        return self.web.host in LOCAL_HOSTS

    # -- derived helpers ---------------------------------------------------
    def market_symbol(self, base: str) -> str:
        """Build the ccxt market symbol for a base asset.

        Futures perpetuals use the settled form ``BASE/QUOTE:QUOTE``; spot uses
        ``BASE/QUOTE``. If the caller already passed a full symbol (contains a
        '/'), it is used verbatim so power users can override the mapping.
        """
        if "/" in base:
            return base
        q = self.market.quote_currency
        return f"{base}/{q}:{q}" if self.futures.enabled else f"{base}/{q}"

    # -- validation --------------------------------------------------------
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

        if not self.market.symbols:
            raise ValueError("market.symbols must list at least one asset")
        if not self.market.quote_currency:
            raise ValueError("market.quote_currency must be set")

        if self.market.source not in ("exchange", "simulated"):
            raise ValueError(
                "market.source must be 'exchange' or 'simulated', got "
                f"'{self.market.source}'"
            )

        if self.market.source == "simulated" and not self.trading.dry_run:
            raise ValueError(
                "market.source: simulated cannot be used with "
                "trading.dry_run: false (there is no real exchange to trade on)."
            )

        f = self.futures
        if f.enabled:
            if f.leverage < 1:
                raise ValueError("futures.leverage must be >= 1")
            if f.margin_mode not in ("isolated", "cross"):
                raise ValueError(
                    "futures.margin_mode must be 'isolated' or 'cross', got "
                    f"'{f.margin_mode}'"
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

        # Exposure guard: never bind the dashboard to a non-local address
        # without a login. Localhost-only may run open (dev/quickstart).
        if not self.host_is_local and not self.auth_active:
            raise ValueError(
                f"Refusing to serve the dashboard on {self.web.host!r} without "
                "authentication. Set DASHBOARD_PASSWORD in .env (and keep "
                "web.auth_enabled: true), or bind web.host to 127.0.0.1."
            )

    # -- runtime-editable settings (the dashboard settings page) ----------
    # Only these fields are exposed for live editing; secrets and plumbing are
    # deliberately excluded.
    def editable_settings(self) -> dict[str, Any]:
        return {
            "market": {
                "quote_currency": self.market.quote_currency,
                "timeframe": self.market.timeframe,
                "symbols": list(self.market.symbols),
            },
            "trading": {
                "dry_run": self.trading.dry_run,
                "poll_interval": self.trading.poll_interval,
            },
            "strategy": {
                "fast_period": self.strategy.fast_period,
                "slow_period": self.strategy.slow_period,
            },
            "risk": {
                "position_size_pct": self.risk.position_size_pct,
                "stop_loss_pct": self.risk.stop_loss_pct,
                "take_profit_pct": self.risk.take_profit_pct,
                "max_daily_loss_pct": self.risk.max_daily_loss_pct,
            },
            "futures": {
                "enabled": self.futures.enabled,
                "leverage": self.futures.leverage,
                "margin_mode": self.futures.margin_mode,
                "allow_short": self.futures.allow_short,
            },
        }

    def apply_updates(self, updates: dict[str, Any]) -> None:
        """Apply a nested dict of edits to the editable fields, then validate.

        Types are coerced to match the current attribute type. Unknown
        sections/keys are ignored. The instance is left unchanged if validation
        fails (caller should catch and report).
        """
        allowed = self.editable_settings()
        targets = {
            "market": self.market, "trading": self.trading,
            "strategy": self.strategy, "risk": self.risk, "futures": self.futures,
        }
        # Work on a snapshot so a failed validate() doesn't half-apply.
        staged: list[tuple[Any, str, Any]] = []
        for section, fields in updates.items():
            if section not in allowed or not isinstance(fields, dict):
                continue
            target = targets[section]
            for key, new_value in fields.items():
                if key not in allowed[section]:
                    continue
                current = getattr(target, key)
                staged.append((target, key, _coerce(current, new_value)))

        originals = [(t, k, getattr(t, k)) for t, k, _ in staged]
        for target, key, value in staged:
            setattr(target, key, value)
        try:
            self.validate()
        except Exception:
            for target, key, value in originals:  # roll back
                setattr(target, key, value)
            raise


def _coerce(current: Any, new_value: Any) -> Any:
    """Coerce ``new_value`` to the type of ``current``."""
    if isinstance(current, bool):
        if isinstance(new_value, str):
            return new_value.strip().lower() in ("1", "true", "yes", "on")
        return bool(new_value)
    if isinstance(current, int):
        return int(new_value)
    if isinstance(current, float):
        return float(new_value)
    if isinstance(current, list):
        if isinstance(new_value, str):
            return [x.strip().upper() for x in new_value.split(",") if x.strip()]
        return [str(x).strip().upper() for x in new_value]
    return new_value


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

    auth = _load_auth_from_env()

    cfg = Config(
        exchange=exchange,
        market=MarketConfig(**_section(raw, "market")),
        trading=TradingConfig(**_section(raw, "trading")),
        strategy=StrategyConfig(**_section(raw, "strategy")),
        risk=RiskConfig(**_section(raw, "risk")),
        futures=FuturesConfig(**_section(raw, "futures")),
        web=WebConfig(**_section(raw, "web")),
        auth=auth,
    )
    cfg.validate()
    return cfg


def _load_auth_from_env() -> AuthConfig:
    """Build dashboard auth from environment secrets.

    DASHBOARD_PASSWORD is hashed in memory (never stored in plaintext beyond
    the process). A missing SECRET_KEY is auto-generated per run — sessions
    then reset on restart, which is fine for a single-user bot; set SECRET_KEY
    in .env to keep logins across restarts.
    """
    from werkzeug.security import generate_password_hash

    password = os.environ.get("DASHBOARD_PASSWORD", "")
    secret = os.environ.get("SECRET_KEY", "")
    ephemeral = not secret
    if ephemeral:
        secret = secrets.token_hex(32)
    return AuthConfig(
        username=os.environ.get("DASHBOARD_USERNAME", "admin"),
        password_hash=generate_password_hash(password) if password else "",
        secret_key=secret,
        ephemeral_secret=ephemeral,
    )


def save_config(cfg: Config, config_path: str | os.PathLike[str] = "config.yaml"
                ) -> None:
    """Persist the non-secret config back to YAML (API keys are never written)."""
    exchange = {k: v for k, v in asdict(cfg.exchange).items()
                if k not in ("api_key", "api_secret", "api_password")}
    data = {
        "exchange": exchange,
        "market": asdict(cfg.market),
        "trading": asdict(cfg.trading),
        "strategy": asdict(cfg.strategy),
        "risk": asdict(cfg.risk),
        "futures": asdict(cfg.futures),
        "web": asdict(cfg.web),
    }
    Path(config_path).write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    )
