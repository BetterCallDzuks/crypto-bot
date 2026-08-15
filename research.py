#!/usr/bin/env python3
"""Scheduled walk-forward research report (CLI).

Runs a walk-forward analysis (and a full-period comparison for context) on the
configured basket, then writes a timestamped Markdown report to ``reports/``.
Intended to run on a schedule (cron) on a machine that can reach the exchange,
so it always tests on fresh, real market data.

Examples:
    ./.venv/bin/python research.py                    # real Binance data
    ./.venv/bin/python research.py --bars 5000 --folds 6
    ./.venv/bin/python research.py --source simulated  # offline dry run

The report is a decision aid, not a signal to trade.
"""

from __future__ import annotations

import argparse
import sys

from cryptobot.config import load_config
from cryptobot.research import generate


def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-forward research report.")
    ap.add_argument("--bars", type=int, default=3000, help="history length")
    ap.add_argument("--folds", type=int, default=4, help="walk-forward folds")
    ap.add_argument("--objective", default="profit_factor",
                    help="in-sample selection metric")
    ap.add_argument("--source", choices=["exchange", "simulated"],
                    default="exchange",
                    help="data source (default exchange = real data)")
    ap.add_argument("--timeframe", default=None,
                    help="override candle timeframe (e.g. 5m, 1h)")
    ap.add_argument("--out", default="reports", help="output directory")
    args = ap.parse_args()

    config = load_config()
    print(f"Running research: {args.source} data, {args.bars} bars, "
          f"{args.folds} folds, symbols {', '.join(config.market.symbols)}…")
    result = generate(config, bars=args.bars, folds=args.folds,
                      objective=args.objective, source=args.source,
                      timeframe=args.timeframe, out_dir=args.out)
    s = result["summary"]
    print(f"  Out-of-sample compounded return: "
          f"{s['oos_compound_return_pct']:+.2f}%  "
          f"({s['folds_profitable']}/{s['folds_total']} folds profitable)")
    print(f"  Report written: {result['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
