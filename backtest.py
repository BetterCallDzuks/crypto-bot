#!/usr/bin/env python3
"""Command-line backtesting for crypto-bot.

Examples:
    ./.venv/bin/python backtest.py --bars 3000
    ./.venv/bin/python backtest.py --strategy rsi --bars 2000
    ./.venv/bin/python backtest.py --compare --bars 3000

Data comes from the source in config.yaml: `simulated` (offline) or `exchange`
(real Binance candles via ccxt). Results ignore fees/funding/slippage and the
daily-loss kill switch — a backtest is a guide, not a guarantee.
"""

from __future__ import annotations

import argparse
import copy
import sys

from cryptobot.backtest import compare_strategies, load_history, run_backtest
from cryptobot.config import load_config


def _fmt(v, d=2):
    return "—" if v is None else f"{v:,.{d}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest crypto-bot strategies.")
    ap.add_argument("--strategy", help="strategy key (default: config value)")
    ap.add_argument("--bars", type=int, default=2000, help="history length")
    ap.add_argument("--compare", action="store_true",
                    help="backtest every strategy and rank them")
    ap.add_argument("--fee", type=float, default=None,
                    help="taker fee %% per fill (default from config, e.g. 0.04)")
    ap.add_argument("--slippage", type=float, default=None,
                    help="slippage %% per fill (default from config, e.g. 0.05)")
    args = ap.parse_args()

    config = load_config()
    fee = None if args.fee is None else args.fee / 100.0
    slip = None if args.slippage is None else args.slippage / 100.0
    print(f"Loading {args.bars} bars from '{config.market.source}' for "
          f"{', '.join(config.market.symbols)} ({config.market.quote_currency}, "
          f"{config.futures.leverage}x)…")
    history = load_history(config, args.bars)

    if args.compare:
        rows = compare_strategies(config, history, fee_rate=fee,
                                  slippage_rate=slip)
        print(f"\n{'Strategy':<22}{'Return%':>10}{'MaxDD%':>10}"
              f"{'Win%':>8}{'Trades':>8}{'PF':>7}{'Sharpe':>9}")
        print("-" * 74)
        for m in rows:
            print(f"{m['label']:<22}{m['total_return_pct']:>10.2f}"
                  f"{m['max_drawdown_pct']:>10.2f}{m['win_rate_pct']:>8.1f}"
                  f"{m['trades']:>8}{_fmt(m['profit_factor']):>7}"
                  f"{m['sharpe']:>9.2f}")
        print("\nRanked by total return. Not a promise of future performance.")
        return 0

    cfg = copy.deepcopy(config)
    if args.strategy:
        cfg.strategy.name = args.strategy
        cfg.strategy.params = {}
        cfg.validate()
    m = run_backtest(cfg, history, fee_rate=fee, slippage_rate=slip)
    print(f"\nStrategy: {m['strategy']}   Bars: {m['bars']}   "
          f"Leverage: {m['leverage']}x")
    print(f"  Costs modeled  fee {m['fee_rate']*100:.3f}% / "
          f"slippage {m['slippage_rate']*100:.3f}% per fill")
    print(f"  Total return   {m['total_return_pct']:+.2f}%   "
          f"(equity {m['starting_equity']:.0f} -> {m['final_equity']:.0f})")
    print(f"  Max drawdown   {m['max_drawdown_pct']:.2f}%")
    print(f"  Win rate       {m['win_rate_pct']:.1f}%  "
          f"({m['wins']}/{m['trades']} trades)")
    print(f"  Profit factor  {_fmt(m['profit_factor'])}")
    print(f"  Fees paid      {m['fees_paid']:.2f} {config.market.quote_currency}")
    print(f"  Sharpe (ann.)  {m['sharpe']:.2f}")
    print(f"  Best / worst   {m['best_trade']:+.2f} / {m['worst_trade']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
