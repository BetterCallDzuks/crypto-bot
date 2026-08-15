"""Walk-forward research report generation (shared by the CLI and dashboard).

Runs a walk-forward analysis plus a full-period comparison on the configured
basket and renders a Markdown report. Always paper-tests — it only reads market
data and never places orders.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backtest import compare_strategies, load_market, walk_forward
from .config import Config

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


def generate(config: Config, bars: int = 3000, folds: int = 4,
             objective: str = "profit_factor", source: str = "exchange",
             timeframe: str | None = None,
             out_dir: str | None = "reports") -> dict[str, Any]:
    """Run the analysis, render the report, optionally write it, return details."""
    cfg = copy.deepcopy(config)
    cfg.market.source = source
    if timeframe:
        cfg.market.timeframe = timeframe
    cfg.trading.dry_run = True
    cfg.validate()

    now = datetime.now(timezone.utc)
    history, funding = load_market(cfg, bars)
    wf = walk_forward(cfg, history, folds=folds, objective=objective,
                      funding_schedule=funding)
    ranking = compare_strategies(cfg, history, funding_schedule=funding)
    report = render(cfg, source, now, wf, ranking,
                    historical_funding=funding is not None)

    path = None
    if out_dir:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"walkforward-{now:%Y%m%d-%H%M}.md"
        path.write_text(report)
        (d / "latest.md").write_text(report)

    return {"report": report, "path": str(path) if path else None,
            "summary": wf["summary"], "generated_at": now.isoformat(timespec="seconds")}


def render(cfg: Config, source: str, now: datetime, wf: dict, ranking: list,
           historical_funding: bool) -> str:
    s = wf["summary"]
    q = cfg.market.quote_currency
    lines = [
        "# Walk-forward research report",
        "",
        f"- **Generated:** {now:%Y-%m-%d %H:%M UTC}",
        f"- **Data:** {source} · {s['num_folds'] + 1} segments of "
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
        "Pick the best strategy on each in-sample window, trade it unseen on "
        "the next. Compounded across folds:",
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
