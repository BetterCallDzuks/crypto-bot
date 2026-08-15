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
    # Stream live mark prices over WebSocket (Binance only) for real-time risk
    # checks and dashboard prices. Ignored for the simulated source.
    live_feed: bool = True


@dataclass
class TradingConfig:
    dry_run: bool = True
    poll_interval: int = 30
    paper_starting_balance: float = 10_000.0


@dataclass
class StrategyConfig:
    # Strategy key from kovanica.strategy.REGISTRY (e.g. confluence,
    # ema_crossover, macd, rsi, bollinger, donchian, sma_crossover).
    name: str = "confluence"
    # Strategy-specific parameters; missing keys fall back to the strategy's
    # own defaults.
    params: dict = field(default_factory=dict)

    def effective_params(self) -> dict:
        """Stored params merged over the strategy's declared defaults."""
        from .strategy import REGISTRY
        defaults = REGISTRY[self.name].default_params if self.name in REGISTRY \
            else {}
        return {**defaults, **self.params}


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
class BacktestConfig:
    # Cost model used when replaying history. Defaults approximate Binance
    # USDT-margined futures: 0.04% taker fee and 0.05% slippage per fill, and a
    # 0.01% funding charge every 8h. Only used by the backtester; live fills
    # and funding are handled by the real exchange.
    fee_rate: float = 0.0004
    slippage_rate: float = 0.0005
    funding_rate: float = 0.0001          # per funding interval (can be < 0)
    funding_interval_hours: float = 8.0


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 4000
    # Require a login for the dashboard. Credentials come from .env, not YAML.
    auth_enabled: bool = True
    # IANA timezone for displaying times on the dashboard. Croatia = CET/CEST.
    timezone: str = "Europe/Zagreb"


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
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
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
        # Building the strategy validates its name and parameters.
        from .strategy import REGISTRY, build_strategy
        if self.strategy.name not in REGISTRY:
            raise ValueError(
                f"Unknown strategy '{self.strategy.name}'. "
                f"Available: {sorted(REGISTRY)}"
            )
        try:
            build_strategy(self.strategy.name, self.strategy.params)
        except ValueError as exc:
            raise ValueError(f"Invalid strategy parameters: {exc}") from exc

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

        for name in ("fee_rate", "slippage_rate"):
            value = getattr(self.backtest, name)
            if not 0 <= value < 1:
                raise ValueError(f"backtest.{name} must be within [0, 1)")
        if not -1 < self.backtest.funding_rate < 1:
            raise ValueError("backtest.funding_rate must be within (-1, 1)")
        if self.backtest.funding_interval_hours <= 0:
            raise ValueError("backtest.funding_interval_hours must be > 0")

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
                "name": self.strategy.name,
                "params": self.strategy.effective_params(),
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
            "web": {
                "timezone": self.web.timezone,
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
            "risk": self.risk, "futures": self.futures, "web": self.web,
        }
        # Snapshot everything we might touch so a failed validate() rolls back.
        strat_before = (self.strategy.name, dict(self.strategy.params))
        staged: list[tuple[Any, str, Any]] = []
        for section, fields in updates.items():
            if section == "strategy" and isinstance(fields, dict):
                self._stage_strategy(fields)
                continue
            if section not in targets or not isinstance(fields, dict):
                continue
            target = targets[section]
            for key, new_value in fields.items():
                if key not in allowed.get(section, {}):
                    continue
                current = getattr(target, key)
                staged.append((target, key, _coerce(current, new_value)))

        originals = [(t, k, getattr(t, k)) for t, k, _ in staged]
        for target, key, value in staged:
            setattr(target, key, value)
        try:
            self.validate()
        except Exception:
            for target, key, value in originals:  # roll back scalars
                setattr(target, key, value)
            self.strategy.name, self.strategy.params = strat_before  # and strategy
            raise

    def _stage_strategy(self, fields: dict[str, Any]) -> None:
        """Apply strategy name/params (params coerced to numbers)."""
        if "name" in fields:
            self.strategy.name = str(fields["name"])
        if "params" in fields and isinstance(fields["params"], dict):
            self.strategy.params = {
                k: _coerce_number(v) for k, v in fields["params"].items()
            }


def _coerce_number(value: Any) -> Any:
    """Best-effort coerce a value to int or float (leave non-numeric as-is)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if any(c in text for c in ".eE"):
                return float(text)
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return value
    return value


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


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (override wins on scalars)."""
    out = dict(base)
    for key, value in override.items():
        if (key in out and isinstance(out[key], dict)
                and isinstance(value, dict)):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def local_config_path(config_path: str | os.PathLike[str]) -> Path:
    """The git-ignored overlay path next to the given config file."""
    return Path(config_path).with_name("config.local.yaml")


def load_config(
    config_path: str | os.PathLike[str] = "config.yaml",
    env_path: str | os.PathLike[str] = ".env",
) -> Config:
    """Load configuration from YAML + environment, validate, and return it.

    A git-ignored ``config.local.yaml`` next to ``config.yaml`` is overlaid on
    top when present, so runtime changes (saved by the dashboard) never touch
    the tracked ``config.yaml`` and never conflict with ``git pull``.
    """
    load_dotenv(env_path)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy the provided config.yaml."
        )

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a mapping at the top level")

    local_path = local_config_path(config_path)
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text()) or {}
        if isinstance(local, dict):
            raw = _deep_merge(raw, local)

    exchange = ExchangeConfig(
        **_section(raw, "exchange"),
        api_key=os.environ.get("EXCHANGE_API_KEY", ""),
        api_secret=os.environ.get("EXCHANGE_API_SECRET", ""),
        api_password=os.environ.get("EXCHANGE_API_PASSWORD", ""),
    )

    auth = _load_auth_from_env()

    # Env overrides let containers set the bind address without editing files.
    web = WebConfig(**_section(raw, "web"))
    if os.environ.get("WEB_HOST"):
        web.host = os.environ["WEB_HOST"]
    if os.environ.get("WEB_PORT"):
        web.port = int(os.environ["WEB_PORT"])

    cfg = Config(
        exchange=exchange,
        market=MarketConfig(**_section(raw, "market")),
        trading=TradingConfig(**_section(raw, "trading")),
        strategy=_parse_strategy(_section(raw, "strategy")),
        risk=RiskConfig(**_section(raw, "risk")),
        futures=FuturesConfig(**_section(raw, "futures")),
        backtest=BacktestConfig(**_section(raw, "backtest")),
        web=web,
        auth=auth,
    )
    cfg.validate()
    return cfg


# Legacy top-level strategy params (pre-`params` schema) folded into params.
_LEGACY_STRATEGY_KEYS = ("fast_period", "slow_period", "signal_period",
                         "period", "oversold", "overbought", "num_std",
                         "threshold")


def _parse_strategy(raw: dict[str, Any]) -> StrategyConfig:
    name = raw.get("name", "confluence")
    params = dict(raw.get("params") or {})
    for key in _LEGACY_STRATEGY_KEYS:
        if key in raw:
            params.setdefault(key, raw[key])
    return StrategyConfig(name=name, params=params)


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
        "backtest": asdict(cfg.backtest),
        "web": asdict(cfg.web),
    }
    Path(config_path).write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    )
