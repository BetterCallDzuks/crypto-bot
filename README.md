# crypto-bot

A safety-first, **multi-symbol** crypto **futures** trading bot with a full web
dashboard.

It trades a basket of large-cap assets (BTC, ETH, XRP, SOL, DOGE, BNB, ADA …)
at once, each independently, on leveraged perpetual futures — going **long** and
flipping **short** on the signals of a selectable trading strategy. Positions
are managed with real risk controls: leverage-aware sizing, stop-loss,
take-profit, a liquidation backstop, and a portfolio-wide daily-loss kill
switch.

Built for **Croatia/EEA**, where USDT isn't available: the quote/margin
currency is **USDC** (or **BNFCR**), not USDT.

The dashboard (default **port 4000**) has four tabs:

- **Overview** — equity, total/daily/realized/unrealized P&L, an equity curve,
  a daily-P&L chart, and the live trade feed.
- **Pairs** — a card per asset with a live price chart, its open position
  (side, leverage, entry, liquidation price, unrealized P&L), and per-pair P&L.
- **Analytics** — win rate, best/worst trade, realized P&L by pair, and a
  daily-P&L breakdown.
- **Settings** — change the bot's parameters live (leverage, risk limits,
  strategy periods, symbols, dry-run, …) and save them without touching files.

The dashboard has an optional **login** (set `DASHBOARD_PASSWORD` in `.env`).
It's required before the dashboard can bind to any non-local address, so it
can't be exposed unprotected. See [DEPLOY.md](DEPLOY.md) for reaching it
remotely via Tailscale.

By default it runs in **paper mode against a simulated market**, so you can
launch it and watch it trade immediately — no network, no API keys, no risk.
Live trading is strictly opt-in. It also trades spot (long-only) with
`futures.enabled: false`.

---

## ⚠️ Safety first — read this

Trading bots move money. This one is built to make the dangerous path
deliberate, not accidental:

- **`trading.dry_run: true` (default)** — orders are simulated in-process.
  Nothing is ever sent to an exchange.
- **`market.source: simulated` (default)** — prices come from a local random
  walk. No exchange connection at all.
- **Live trading requires two explicit steps:** set `trading.dry_run: false`
  *and* provide real API keys in `.env`. Without keys, the bot refuses to
  start in live mode.
- Even in live mode, keep **`exchange.sandbox: true`** to trade against the
  exchange's testnet until you've validated everything.

**Leverage cuts both ways.** Futures positions can be **liquidated**: at 5x a
~20% adverse move wipes the position's margin (isolated margin caps the loss at
that position's collateral — it can't drain the rest of your balance). Higher
leverage means a smaller move liquidates you. The stop-loss is set to trigger
well before liquidation, but gaps and slippage are real. Start at low leverage.

No strategy is guaranteed to be profitable. The included strategies are
well-known building blocks — treat them as a starting point, not financial
advice. Never risk money you can't afford to lose.

---

## Quick start (paper mode, works offline)

```bash
./setup.sh          # creates .venv, installs deps, makes .env from template
./.venv/bin/python run.py
```

Open the dashboard at **http://127.0.0.1:4000**. With the default config the
bot trades a *simulated* basket, so prices update and trades appear within a
few polling intervals — no network or API keys required.

## Running under PM2 (recommended)

[PM2](https://pm2.keymetrics.io/) keeps the bot running and restarts it on
crash or reboot. An `ecosystem.config.js` is included:

```bash
./setup.sh                        # one-time
pm2 start ecosystem.config.js     # start bot + dashboard
pm2 logs crypto-bot               # follow logs
pm2 restart crypto-bot            # after changing symbols or quote currency
pm2 save && pm2 startup           # survive reboots
```

PM2 runs `run.py` through the project's `./.venv/bin/python`, so no global
Python packages are needed.

For a full VPS walkthrough — prerequisites, PM2 setup, and how to reach the
dashboard securely from anywhere (SSH tunnel, Tailscale, or an HTTPS reverse
proxy) — see **[DEPLOY.md](DEPLOY.md)**.

## Running the tests

```bash
./.venv/bin/pip install pytest
./.venv/bin/pytest
```

The suite (84 tests) covers every strategy and its indicators, side-aware risk
and liquidation, config validation + live settings updates, multi-symbol
portfolio accounting, the backtester (costs, funding, walk-forward), and full
engine round-trips (long/short flips, stop-loss, liquidation) using a fake
exchange — so it runs without any network access.

---

## Strategies

Pick one in `config.yaml` (`strategy.name`) or from the **Settings** tab, where
each strategy exposes its own parameters. All operate on close prices and emit
buy / sell / hold signals; the engine turns those into long/short entries and
exits under the risk rules.

| Key             | Type          | Idea                                                        |
|-----------------|---------------|-------------------------------------------------------------|
| `confluence`    | Ensemble      | Trades only when several strategies (EMA, MACD, RSI) agree — the most robust default. `threshold` = how many must agree (1–3). |
| `ema_crossover` | Trend         | Fast/slow exponential moving-average crossover; reacts faster than SMA. |
| `sma_crossover` | Trend         | Fast/slow simple moving-average crossover; the classic baseline. |
| `macd`          | Momentum      | MACD line crossing its signal line. |
| `rsi`           | Mean reversion| Enter as RSI leaves oversold / overbought. |
| `bollinger`     | Mean reversion| Buy/sell as price re-enters the Bollinger bands. |
| `donchian`      | Breakout      | Break of the N-bar high/low (Turtle-style trend capture). |

There is no "best" strategy that always wins — market regimes change. The
**confluence** ensemble is the default because requiring agreement filters out
many false signals, at the cost of trading less often. Backtest and paper-trade
any choice before committing real funds.

---

## Backtesting

Before trusting a strategy with money, test it on history. The backtester
replays past candles through the **exact live engine** (same sizing, stops,
take-profit, leverage and flips), so results reflect how the bot would really
behave — not a separate toy model.

**From the dashboard** — the **Backtest** tab: pick a strategy and history
length, hit *Run backtest* for a full metric breakdown + equity curve, or
*Compare all strategies* to rank every strategy on the same data.

**From the CLI:**

```bash
./.venv/bin/python backtest.py --strategy rsi --bars 3000
./.venv/bin/python backtest.py --compare --bars 3000
```

Data comes from `market.source`: `simulated` (offline) or `exchange` (real
Binance candles via ccxt). Reported metrics: total return, **max drawdown**,
win rate, **profit factor** (gross win ÷ gross loss), average/best/worst trade,
**fees paid**, **funding paid**, and an annualized Sharpe ratio.

**Costs are modeled.** Every fill is charged a configurable taker fee and
slippage, and open positions pay/receive **perpetual funding** each interval
(defaults: 0.04% fee, 0.05% slippage — set in `config.yaml` under `backtest`, or
per-run in the tab / with `--fee`, `--slippage`, `--funding`). This matters
enormously: a high-churn strategy that looks green at zero cost can turn deeply
negative once costs are included — exactly the trap you want the backtester to
expose *before* real money is involved.

**Funding uses real history on real data.** With `market.source: exchange`, the
backtester pulls each symbol's actual historical funding rates from Binance
(`fetch_funding_rate_history`) and applies them at their real timestamps — so
longs and shorts are charged/paid what they truly would have been. The flat
`funding_rate` is only a fallback for the offline `simulated` source. Each
result reports whether funding was `historical` or `flat`.

### Walk-forward analysis

A single backtest is easy to fool yourself with — pick whatever strategy
happened to win on that stretch of history and you've *curve-fit the past*.
Walk-forward guards against it: history is split into consecutive segments; on
each in-sample segment the bot selects the best strategy, then trades that
choice **unseen** on the next segment. Out-of-sample returns are compounded.

```bash
./.venv/bin/python backtest.py --walkforward --bars 3000 --folds 4
```

Or use the **Run walk-forward** button on the Backtest tab. If picking the past
winner keeps losing out-of-sample (it usually does), that is the honest signal
that the "edge" was noise — trust the out-of-sample number, not the in-sample
one.

> **What a backtest still isn't.** It measures one slice of the past, doesn't
> model order-book depth (a big order slips more than a flat rate), and disables
> the daily-loss kill switch during replay. A great backtest is necessary but
> never sufficient — it does not promise future profit. Treat it as a filter for
> *bad* strategies, then paper-trade the survivors before going live.

---

## Configuration

All non-secret settings live in `config.yaml`; secrets live in `.env`.

Most fields can also be changed live from the **Settings** tab. Those changes
are written to a git-ignored **`config.local.yaml`** overlay (which wins over
`config.yaml` for any field it sets), so the tracked `config.yaml` is never
modified and `git pull` never conflicts with your live settings. Symbols and
quote currency need a restart.

| Section     | Key                     | Meaning                                                        |
|-------------|-------------------------|----------------------------------------------------------------|
| `market`    | `source`                | `simulated` (offline) or `exchange` (live ccxt data)           |
| `market`    | `quote_currency`        | Margin/quote asset — **USDC** or **BNFCR** (EEA; not USDT)     |
| `market`    | `symbols`               | List of base assets to trade (BTC, ETH, XRP, SOL, DOGE, …)     |
| `market`    | `timeframe`             | Candle size for the strategy (`1m`, `5m`, `1h`, …)             |
| `market`    | `live_feed`             | Stream live mark prices over WebSocket (Binance) for real-time risk checks |
| `exchange`  | `id`                    | Any [ccxt](https://github.com/ccxt/ccxt) exchange id (binance) |
| `exchange`  | `sandbox`               | Use the exchange testnet when available                        |
| `futures`   | `enabled`               | Trade leveraged perpetual futures; `false` = spot (long-only)  |
| `futures`   | `leverage`              | Leverage multiplier, e.g. `5` for 5x                           |
| `futures`   | `margin_mode`           | `isolated` (caps loss to the position) or `cross`              |
| `futures`   | `allow_short`           | Allow flipping to a short on a bearish crossover               |
| `trading`   | `dry_run`               | **Master safety switch** — simulate orders when true           |
| `trading`   | `poll_interval`         | Seconds between strategy evaluations                           |
| `strategy`  | `fast_period` / `slow_period` | Moving-average windows for the crossover                 |
| `risk`      | `position_size_pct`     | Fraction of the shared balance posted as margin per position   |
| `risk`      | `stop_loss_pct`         | Exit when price moves this far *against* the position          |
| `risk`      | `take_profit_pct`       | Exit when price moves this far *in favor* of the position      |
| `risk`      | `max_daily_loss_pct`    | Halt new entries once the day's drawdown hits this             |
| `web`       | `host` / `port`         | Dashboard bind address (default `127.0.0.1:4000`)              |
| `web`       | `auth_enabled`          | Require a dashboard login (password comes from `.env`)         |
| `web`       | `timezone`              | IANA timezone for on-screen times (default `Europe/Zagreb`)    |

The ccxt market symbol is derived per asset from the quote currency:
`BTC` + `USDC` → `BTC/USDC:USDC` for a perpetual. You can also put a full ccxt
symbol directly in the `symbols` list to override the mapping.

## Going live (advanced)

1. `cp .env.example .env` (setup.sh does this for you) and fill in your Binance
   `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET`. `.env` is git-ignored — never
   commit real keys.
2. In `config.yaml` set `market.source: exchange` and `futures.enabled: true`,
   with `market.quote_currency: USDC` (or `BNFCR`). Make sure your Binance
   account holds that asset in the **futures** wallet.
3. Validate against the testnet first: keep `exchange.sandbox: true` and
   `trading.dry_run: true`, and confirm prices and trades look sane. In live
   mode the bot calls `set_leverage` / `set_margin_mode` per symbol on startup.
4. Only then, if you fully understand the risk, set `trading.dry_run: false`
   (or untick Dry-run in the Settings tab).

> **Quote currency / symbols on Binance EEA.** USDT is unavailable in Croatia.
> This bot defaults to USDC-margined perpetuals (`BTC/USDC:USDC`). Binance also
> offers **BNFCR** as an EEA settlement asset — set `quote_currency: BNFCR` to
> use it. Exact contract symbols vary by account and region, so once connected,
> confirm the tradable symbols on your Binance futures account and adjust
> `market.symbols` (you can drop a full ccxt symbol straight into the list).

> Some environments (locked-down CI/sandboxes) block outbound connections to
> exchange APIs. If `market.source: exchange` fails with a network error,
> that's the network policy — use `simulated` mode there.

---

## Architecture

```
run.py                  Entrypoint: wires config → exchange → engine → web
backtest.py             CLI backtester (single / compare / walk-forward)
research.py             Scheduled walk-forward report generator (cron)
ecosystem.config.js     PM2 process definition
setup.sh                One-time venv + deps + .env bootstrap
cryptobot/
  config.py             Typed config; validation, live updates, persistence
  strategy.py           Strategy library + indicators (registry & factory)
  risk.py               Leverage sizing, side-aware exits, liquidation, limits
  state.py              Multi-symbol portfolio: positions, P&L, charts data
  exchange.py           Multi-symbol ccxt wrapper; futures + dry-run order gate
  simulated.py          Offline multi-asset random-walk market (same interface)
  backtest.py           Replays history through the live engine; metrics
  trader.py             Background engine: per-symbol loop, runtime reload
  web/app.py            Flask app: state, live settings, and stop endpoints
  web/templates/        The dashboard UI (Overview/Pairs/Analytics/Settings)
tests/                  Strategy, risk, config, state, and engine tests (41)
```

**Design notes**

- The strategy, risk, and state modules are pure and deterministic, so every
  trading rule (including long/short P&L and liquidation) is unit-tested.
- One portfolio holds a shared quote balance and an independent position per
  symbol; sizing and the daily-loss kill switch are portfolio-wide.
- Spot and futures share one accounting model: spot is simply the leverage-1,
  always-long special case of the margin equations in `state.py`.
- The engine depends only on a small duck-typed exchange interface
  (`fetch_closes`, `create_order`, keyed by base asset), so the same code
  drives the real exchange, the simulated market, and the test fake.
- Order placement has exactly one gate — `dry_run` in `exchange.py` — so
  there is a single, auditable place where a real order can be sent.
- Charts are hand-rolled inline SVG (no chart library, no CDN), so the
  dashboard works fully offline.
