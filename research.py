#!/usr/bin/env python3
"""Scheduled walk-forward research report.

Runs a walk-forward analysis (and a full-period strategy comparison for
context) on the configured basket, then writes a timestamped Markdown report to
``reports/``. Intended to be run on a schedule (cron) on a machine that can
reach the exchange, so it always tests on fresh, real market data.

Examples:
    ./.venv/bin/python research.py                    # real Binance data
    ./.venv/bin/python research.py --bars 5000 --folds 6
    ./.venv/bin/python research.py --source simulated  # offline dry run

The report is a decision aid, not a signal to trade. Read REPORT_DISCLAIMER.
"""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptobot.backtest import compare_strategies, load_market, walk_forward
from cryptobot.config import load_config

REPORT_DISCLAIMER = (
    "This report measures the past on one data set, net of fees, slippage and "
    "funding. It does not model order-book depth and is **not** a prediction. "
    "The out-of-sample (walk-forward) numbers are the honest ones; the "
    "full-period ranking is prone to curve-fitting. Do not deploy a strategy on "
    "the strength of a single report — require it to survive walk-forward across "
    "several periods, then paper-trade before risking funds."
)


def _fmt(v, d=2):
    return "—" if v is None else f"{v:,.{d}f}"


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
    cfg = copy.deepcopy(config)
    cfg.market.source = args.source
    if args.timeframe:
        cfg.market.timeframe = args.timeframe
    cfg.trading.dry_run = True
    cfg.validate()

    now = datetime.now(timezone.utc)
    print(f"[{now:%Y-%m-%d %H:%M UTC}] research: {args.source} data, "
          f"{args.bars} bars, {args.folds} folds, timeframe "
          f"{cfg.market.timeframe}, symbols {', '.join(cfg.market.symbols)}")

    history, funding = load_market(cfg, args.bars)
    wf = walk_forward(cfg, history, folds=args.folds, objective=args.objective,
                      funding_schedule=funding)
    ranking = compare_strategies(cfg, history, funding_schedule=funding)

    report = _render(cfg, args, now, wf, ranking,
                     historical_funding=funding is not None)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"walkforward-{now:%Y%m%d-%H%M}.md"
    path.write_text(report)
    latest = out_dir / "latest.md"
    latest.write_text(report)

    s = wf["summary"]
    print(f"  Out-of-sample compounded return: "
          f"{s['oos_compound_return_pct']:+.2f}%  "
          f"({s['folds_profitable']}/{s['folds_total']} folds profitable)")
    print(f"  Report written: {path}")
    return 0


def _render(cfg, args, now, wf, ranking, historical_funding) -> str:
    s = wf["summary"]
    q = cfg.market.quote_currency
    lines = [
        f"# Walk-forward research report",
        "",
        f"- **Generated:** {now:%Y-%m-%d %H:%M UTC}",
        f"- **Data:** {args.source} · {s['num_folds'] + 1} segments of "
        f"{s['segment_bars']} bars · timeframe {cfg.market.timeframe}",
        f"- **Symbols:** {', '.join(cfg.market.symbols)} ({q}, "
        f"{cfg.futures.leverage}x)",
        f"- **Costs:** fee {cfg.backtest.fee_rate*100:.3f}% · slippage "
        f"{cfg.backtest.slippage_rate*100:.3f}% · funding "
        f"{'historical' if historical_funding else 'flat'}",
        f"- **Selection objective:** {s['objective']}",
        "",
        "## Out-of-sample (the honest result)",
        "",
        f"Pick the best strategy on each in-sample window, trade it unseen on "
        f"the next. Compounded across folds:",
        "",
        f"- **OOS compounded return:** {s['oos_compound_return_pct']:+.2f}%",
        f"- **Profitable folds:** {s['folds_profitable']} / {s['folds_total']}",
        f"- **Average fold return:** {s['avg_fold_return_pct']:+.2f}%",
        "",
        "| Fold | In-sample winner | OOS return | OOS PF | OOS max DD | Trades |",
        "|-----:|------------------|-----------:|-------:|-----------:|-------:|",
    ]
    for r in wf["folds"]:
        lines.append(
            f"| {r['fold']} | {r['train_label']} | "
            f"{r['test_return_pct']:+.2f}% | {_fmt(r['test_profit_factor'])} | "
            f"{r['test_max_drawdown_pct']:.2f}% | {r['test_trades']} |")

    lines += [
        "",
        "## Full-period ranking (context only — prone to curve-fitting)",
        "",
        "| Strategy | Return | Max DD | Win rate | Trades | PF | Sharpe |",
        "|----------|-------:|-------:|---------:|-------:|---:|-------:|",
    ]
    for m in ranking:
        lines.append(
            f"| {m['label']} | {m['total_return_pct']:+.2f}% | "
            f"{m['max_drawdown_pct']:.2f}% | {m['win_rate_pct']:.1f}% | "
            f"{m['trades']} | {_fmt(m['profit_factor'])} | {m['sharpe']:.2f} |")

    lines += ["", "---", "", f"> {REPORT_DISCLAIMER}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
