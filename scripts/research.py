"""候補の戦略を、同じ物差しで並べる。

# 開発期間で候補を比べる（ロックボックスは見ない）
python scripts/research.py

# 選んだ1つを、ロックボックスで一度だけ評価する（同じデータでは二度目は拒否される）
python scripts/research.py --run-lockbox

手順:
  1. データの最後の ``--lockbox-days`` 営業日を取り置く（開発期間では見ない）
  2. 開発期間で全候補を同じ区間・同じコストで評価する
  3. 試行回数 N から「何もなくても出る Sharpe」を出し、超えたかを見る
  4. 最良の1つを、ロックボックスで **一度だけ** 評価する
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from jqbt import compute
from jqbt.core.monthly import monthly_returns, monthly_summary
from jqbt.core.selection import expected_max_sharpe
from jqbt.infrastructure import experiment_log
from jqbt.infrastructure.env import load_env
from jqbt.interfaces.composition import DataRequest, build_runner, default_extra_spread
from jqbt.interfaces.composition import load_panel as _load
from jqbt.strategies import REGISTRY, RandomEntry

load_env()

CANDIDATES = ["momentum", "reversal", "lowvol", "breakout", "regime_hold"]
TRADING_DAYS = 245


def trim(result, start):
    """``start`` 以降だけを残す。戦略の助走期間（まだ何も持たない期間）を評価から外す。"""
    keep = lambda d: d >= start
    return dataclasses.replace(
        result,
        snapshots=tuple(s for s in result.snapshots if keep(s.date)),
        fills=tuple(f for f in result.fills if keep(f.date)),
        round_trips=tuple(t for t in result.round_trips if keep(t.exit_date)),
        rejections=tuple(r for r in result.rejections if keep(r.date)),
    )


def daily_sharpe(result) -> float | None:
    equity = pd.Series([s.equity for s in result.snapshots], dtype=float)
    returns = equity.pct_change().dropna()
    if len(returns) < 2 or returns.std() == 0:
        return None
    return float(returns.mean() / returns.std())


def evaluate(runner, dates, strategy, eval_start_idx, end_idx):
    """``dates[eval_start_idx]`` から ``dates[end_idx]`` までを評価する。

    戦略は自分の助走ぶん手前から走らせ、評価区間の頭から持ち始めるようにそろえる。
    """
    warmup = getattr(strategy, "warmup", 0)
    start_idx = max(eval_start_idx - warmup, 0)
    result = runner.slice(dates[start_idx], dates[end_idx]).run(strategy)
    result = trim(result, dates[eval_start_idx])
    return result, compute(result)


def market_reference(panel, start, end):
    """全銘柄の等ウェイト平均。**取引はできない**参考値（地合いの目安）。"""
    close = panel["close"].unstack("symbol").loc[pd.Timestamp(start) : pd.Timestamp(end)]
    daily = close.pct_change().clip(-0.2, 0.2).mean(axis=1).dropna()
    equity = (1 + daily).cumprod()
    sharpe = daily.mean() / daily.std() if daily.std() else None
    drawdown = float((equity / equity.cummax() - 1).min())
    return {"total_return": float(equity.iloc[-1] - 1), "sharpe_daily": sharpe, "max_drawdown": drawdown}


def row(label, metrics, sharpe_d, summary):
    return (
        f"{label:<22}{metrics.total_return:>+8.1%}{metrics.max_drawdown:>+9.1%}"
        f"{(sharpe_d or 0) * np.sqrt(TRADING_DAYS):>8.2f}{metrics.trade_count:>6}"
        f"{summary.get('non_loss_ratio', 0):>8.0%}{summary.get('reach_target_ratio', 0):>8.0%}"
        f"{summary.get('worst', 0):>+9.1%}"
    )


HEADER = f"{'候補':<22}{'リターン':>8}{'最大DD':>9}{'Sharpe':>8}{'取引':>6}{'非赤字月':>8}{'目標月':>8}{'最悪月':>9}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--lot-size", type=int, default=1)
    parser.add_argument("--costs", default="realistic")
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--lockbox-days", type=int, default=100)
    parser.add_argument("--prior-trials", type=int, default=2, help="以前に見た結果の数（試行に加える）")
    parser.add_argument("--target-monthly", type=float, default=0.02)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--null-runs", type=int, default=20)
    parser.add_argument("--run-lockbox", action="store_true")
    args = parser.parse_args()

    panel = _load(DataRequest(synthetic=False, start=None, end=None, symbols=None, regime="mixed"))
    runner = build_runner(panel, args.cash, args.costs, True, args.lot_size)
    dates = runner.dates
    spread = default_extra_spread(args.lot_size)
    print(f"資金 {args.cash:,.0f}円 / 売買単位 {args.lot_size}株 / 追加スプレッド 片道{spread:.2%}（仮定）/ "
          f"最大{args.max_positions}銘柄 / コスト: {args.costs}")

    specs = {name: REGISTRY[name] for name in CANDIDATES}
    grid = [
        (name, dict(zip(spec.grid, values), max_positions=args.max_positions))
        for name, spec in specs.items()
        for values in itertools.product(*spec.grid.values())
    ]
    trials = len(grid) + args.prior_trials

    common_warmup = max(specs[name].factory(params).warmup for name, params in grid)
    dev_start = common_warmup
    dev_end = len(dates) - args.lockbox_days - 1
    lock_start = len(dates) - args.lockbox_days
    data_end = str(dates[-1])
    print(f"開発期間 {dates[dev_start]} 〜 {dates[dev_end]}（{dev_end - dev_start + 1}営業日）/ "
          f"ロックボックス {dates[lock_start]} 〜 {dates[-1]}（{args.lockbox_days}営業日・未使用）")
    print(f"候補 {len(grid)}通り + 以前に見た結果 {args.prior_trials} = 試行回数 N={trials}\n")

    # --- 基準線 ---
    reference = market_reference(panel, dates[dev_start], dates[dev_end])
    null = []
    for seed in range(args.null_runs):
        strategy = RandomEntry(holding_days=20, max_positions=args.max_positions, seed=seed)
        result, metrics = evaluate(runner, dates, strategy, dev_start, dev_end)
        null.append((metrics.total_return, daily_sharpe(result) or 0.0))
    null_returns = np.array([r for r, _ in null])
    null_sharpes = np.array([s for _, s in null]) * np.sqrt(TRADING_DAYS)

    # --- 候補 ---
    rows = []
    for name, params in grid:
        strategy = specs[name].factory(params)
        result, metrics = evaluate(runner, dates, strategy, dev_start, dev_end)
        sharpe_d = daily_sharpe(result)
        summary = monthly_summary(monthly_returns(result.snapshots), args.target_monthly)
        rows.append((strategy.name, params, result, metrics, sharpe_d, summary))
        experiment_log.append({
            "kind": "dev", "strategy": name, "params": params, "label": strategy.name,
            "window": [str(dates[dev_start]), str(dates[dev_end])], "data_end": data_end,
            "lot_size": args.lot_size, "cash": args.cash, "costs": args.costs,
            "total_return": metrics.total_return, "sharpe_daily": sharpe_d,
            "trades": metrics.trade_count, "max_drawdown": metrics.max_drawdown,
        })

    sample = dev_end - dev_start + 1
    threshold = (expected_max_sharpe(trials, sample) or 0.0)
    print("【開発期間】月次: 非赤字月 = 元本を割らなかった月の割合 / 目標月 = 月次" f"{args.target_monthly:+.0%}以上の月の割合")
    print(HEADER)
    rows.sort(key=lambda r: (r[4] is None, -(r[4] or 0)))
    for label, params, result, metrics, sharpe_d, summary in rows:
        flag = "" if metrics.trade_count >= args.min_trades else "  (取引数不足)"
        print(row(label, metrics, sharpe_d, summary) + flag)
    print("-" * 90)
    print(f"{'ランダム（中央値）':<22}{np.median(null_returns):>+8.1%}{'':>9}{np.median(null_sharpes):>8.2f}")
    print(f"{'ランダム（5〜95%）':<22}{np.percentile(null_returns, 5):>+8.1%} 〜 {np.percentile(null_returns, 95):+.1%}")
    ref_sharpe = (reference["sharpe_daily"] or 0) * np.sqrt(TRADING_DAYS)
    print(f"{'市場の等ウェイト(参考)':<22}{reference['total_return']:>+8.1%}{reference['max_drawdown']:>+9.1%}{ref_sharpe:>8.2f}  ※取引不可")
    print(f"\n何もなくても N={trials} 回試せば出る Sharpe の目安: {threshold * np.sqrt(TRADING_DAYS):.2f}（年率換算）")

    eligible = [r for r in rows if r[3].trade_count >= args.min_trades and r[4] is not None]
    if not eligible:
        print("\n取引数の条件（%d件以上）を満たす候補がありません。ロックボックスは評価しません。" % args.min_trades)
        return 0
    best = eligible[0]
    beats = best[4] > threshold
    print(f"\n開発期間の最良: {best[0]}（Sharpe {best[4] * np.sqrt(TRADING_DAYS):.2f}）→ "
          + ("N回試した水準を超えている" if beats else "N回試した水準を超えていない（まぐれで説明がつく）"))

    if not args.run_lockbox:
        print("ロックボックスは未使用です（--run-lockbox で、この最良候補を一度だけ評価します）。")
        return 0
    if experiment_log.lockbox_used(data_end):
        print("このデータのロックボックスは評価済みです。見た後の再評価は合わせ込みになるため、拒否します。")
        return 1

    label, params, *_ = best
    name = next(n for n, p in grid if specs[n].factory(p).name == label)
    strategy = specs[name].factory(params)
    result, metrics = evaluate(runner, dates, strategy, lock_start, len(dates) - 1)
    sharpe_d = daily_sharpe(result)
    summary = monthly_summary(monthly_returns(result.snapshots), args.target_monthly)
    ref = market_reference(panel, dates[lock_start], dates[-1])
    experiment_log.append({
        "kind": "lockbox", "label": label, "params": params, "data_end": data_end,
        "window": [str(dates[lock_start]), data_end], "total_return": metrics.total_return,
        "sharpe_daily": sharpe_d, "trades": metrics.trade_count, "max_drawdown": metrics.max_drawdown,
    })
    print("\n【ロックボックス】（これで評価は終わり。見た結果で戦略を直さないこと）")
    print(HEADER)
    print(row(label, metrics, sharpe_d, summary))
    print(f"{'市場の等ウェイト(参考)':<22}{ref['total_return']:>+8.1%}{ref['max_drawdown']:>+9.1%}  ※取引不可")
    if metrics.warnings:
        print("警告: " + " / ".join(metrics.warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
