# crypto-bot

A safety-first crypto trading bot with a live web dashboard.

It polls the market on a schedule, runs a moving-average crossover strategy,
and manages positions with real risk controls (position sizing, stop-loss,
take-profit, and a daily-loss kill switch). A Flask dashboard shows equity,
P&L, the open position, and recent trades in real time.

By default it runs in **paper mode against a simulated market**, so you can
launch it and watch it work immediately — no network access, no API keys, and
no money at risk. Live trading is strictly opt-in.

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

No strategy is guaranteed to be profitable. The included SMA crossover is a
simple, well-known baseline — treat it as a starting point, not financial
advice. Never risk money you can't afford to lose.

---

## Quick start (paper mode, works offline)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python run.py
```

Open the dashboard at **http://127.0.0.1:5000**. With the default config the
bot trades a simulated market, so you'll see prices update and trades appear
within a few polling intervals.

## Running the tests

```bash
source .venv/bin/activate
pip install pytest
pytest
```

The suite covers the strategy, the risk rules, config validation, and a full
engine round-trip (signal → sizing → simulated fill → stop-loss exit) using a
fake exchange — so it runs without any network access.

---

## Configuration

All non-secret settings live in `config.yaml`; secrets live in `.env`.

| Section     | Key                     | Meaning                                                        |
|-------------|-------------------------|----------------------------------------------------------------|
| `market`    | `source`                | `simulated` (offline) or `exchange` (live ccxt data)           |
| `market`    | `symbol` / `timeframe`  | Trading pair and candle size                                   |
| `exchange`  | `id`                    | Any [ccxt](https://github.com/ccxt/ccxt) exchange id           |
| `exchange`  | `sandbox`               | Use the exchange testnet when available                        |
| `trading`   | `dry_run`               | **Master safety switch** — simulate orders when true           |
| `trading`   | `poll_interval`         | Seconds between strategy evaluations                           |
| `strategy`  | `fast_period` / `slow_period` | Moving-average windows for the crossover                 |
| `risk`      | `position_size_pct`     | Fraction of balance deployed per position                      |
| `risk`      | `stop_loss_pct`         | Exit if price falls this far below entry                       |
| `risk`      | `take_profit_pct`       | Exit if price rises this far above entry                       |
| `risk`      | `max_daily_loss_pct`    | Halt new entries once the day's drawdown hits this             |

## Going live (advanced)

1. `cp .env.example .env` and fill in `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET`
   (plus `EXCHANGE_API_PASSWORD` if your exchange requires one). `.env` is
   git-ignored — never commit real keys.
2. In `config.yaml` set `market.source: exchange`.
3. Validate against the testnet first: keep `exchange.sandbox: true` and
   `trading.dry_run: true`, and confirm data flows and trades look sane.
4. Only then, if you fully understand the risk, set `trading.dry_run: false`.

> Some environments (including locked-down CI/sandboxes) block outbound
> connections to exchange APIs. If `market.source: exchange` fails with a
> network error, that's the network policy — use `simulated` mode there.

---

## Architecture

```
run.py                  Entrypoint: wires config → exchange → engine → web
cryptobot/
  config.py             Typed config from YAML + .env, with validation
  strategy.py           Pure SMA-crossover strategy (pluggable via factory)
  risk.py               Position sizing, stop-loss/take-profit, daily limit
  state.py              Thread-safe portfolio/position/trade state
  exchange.py           ccxt wrapper; dry-run gate on all order placement
  simulated.py          Offline random-walk market (same interface)
  trader.py             Background engine loop tying it all together
  web/app.py            Flask app: dashboard + JSON state + stop endpoint
  web/templates/        The dashboard UI
tests/                  Strategy, risk, config, and engine tests
```

**Design notes**

- The strategy and risk modules are pure and deterministic, so every trading
  rule is unit-tested exhaustively.
- The engine depends only on a small duck-typed exchange interface
  (`fetch_closes`, `create_market_buy`, `create_market_sell`), which is why
  the same code drives the real exchange, the simulated market, and the test
  fake with no branching.
- Order placement has exactly one gate — `dry_run` in `exchange.py` — so
  there is a single, auditable place where a real order can be sent.
