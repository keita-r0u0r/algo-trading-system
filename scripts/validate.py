"""Out-of-Sample 検証とウォークフォワード。

    # ホールドアウト（前7割で決めて、後3割で検証）
    python scripts/validate.py holdout --strategy sma_cross --synthetic

    # ウォークフォワード（本命）
    python scripts/validate.py walkforward --strategy sma_cross --synthetic \
        --train 250 --test 60

    # 学習の窓を伸ばしていく方式
    python scripts/validate.py walkforward --strategy sma_cross --synthetic --anchored

**報告してよいのは検証期間の数字だけです。** 学習期間の成績は
「何通りか試した中の最良」なので、必ず良く見えます。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.infrastructure.env import load_env

load_env(Path(__file__).resolve().parent)

from jqbt.application.optimization import optimize
from jqbt.core.metrics import compute, format_report
from jqbt.core.splitting import holdout, walk_forward
from jqbt.interfaces.composition import (
    DataRequest,
    build_runner,
    build_walkforward,
    load_panel,
    resolve_objective,
    resolve_strategy,
)
from jqbt.interfaces.reporting import format_walkforward, format_windows, num, pct


def _panel(args):
    request = DataRequest(
        synthetic=args.synthetic,
        start=args.start,
        end=args.end,
        symbols=args.symbols,
        regime=args.regime,
    )
    if args.synthetic:
        print("合成データを使用します。**成績に意味はありません**（仕組みの確認用）\n")
    panel = load_panel(request)
    days = len(set(panel.index.get_level_values("date")))
    symbols = len(set(panel.index.get_level_values("symbol")))
    print(f"パネル: {days}営業日 × {symbols}銘柄 = {len(panel):,}行\n")
    return panel


# ----------------------------------------------------------------------
def run_holdout(args) -> None:
    panel = _panel(args)
    runner = build_runner(
        panel, args.cash, args.costs, not args.ignore_lot_size, args.lot_size, args.extra_spread
    )
    spec = resolve_strategy(args.strategy)
    objective, objective_name = resolve_objective(args.objective)

    window = holdout(runner.dates, train_ratio=args.train_ratio)
    print(format_windows((window,)))
    print()

    # --- 学習期間でパラメータを決める ---
    train_runner = runner.slice(window.train_start, window.train_end)
    result = optimize(
        train_runner, spec.factory, spec.grid, objective, args.minimum_trades
    )
    selection = result.selection
    print("【学習期間で選んだパラメータ】")
    print(f"  {selection.best.key() or '（パラメータなし）'}")
    print(f"  スコア({objective_name})  {num(selection.best.score)}")
    print(f"  試行回数              {selection.trials}通り")
    print(f"  偶然の期待最大値      {num(selection.expected_best_under_null)}")
    print(f"  → {selection.note}")
    print()

    # --- 検証期間で適用する ---
    test_start = _with_warmup(runner.dates, window.test_start, spec.warmup_days)
    test_runner = runner.slice(test_start, window.test_end)
    test_result = test_runner.run(spec.factory(selection.params), params=selection.params)
    test_metrics = compute(test_result)

    print(format_report(test_metrics, "検証期間（報告してよいのはこちら）"))

    if result.best_metrics:
        print()
        print("【学習 vs 検証】")
        train_score = objective(result.best_metrics)
        test_score = objective(test_metrics)
        print(f"  学習 {num(train_score)} → 検証 {num(test_score)}")
        if train_score and test_score is not None and train_score > 0:
            drop = (train_score - test_score) / train_score
            print(f"  劣化率 {pct(drop, 0)}")
            if drop > 0.7:
                print("  ! 7割以上落ちています。学習期間に合わせ込んでいる可能性が高いです")


def run_walkforward(args) -> None:
    panel = _panel(args)
    runner = build_runner(
        panel, args.cash, args.costs, not args.ignore_lot_size, args.lot_size, args.extra_spread
    )
    spec = resolve_strategy(args.strategy)

    windows = walk_forward(
        runner.dates,
        train_size=args.train,
        test_size=args.test,
        step=args.step,
        anchored=args.anchored,
    )
    if not windows:
        raise SystemExit(
            f"窓を作れませんでした。営業日{len(runner.dates)}日に対して "
            f"学習{args.train}日 + 検証{args.test}日 が長すぎます"
        )

    print(format_windows(windows))
    print(f"\n各窓で {spec.trial_count} 通りのパラメータを試します"
          f"（合計 {spec.trial_count * len(windows)} 回のバックテスト）…\n")

    service = build_walkforward(runner, spec, args.objective, args.minimum_trades)
    report = service.execute(windows)
    print(format_walkforward(report))


def _with_warmup(dates, start, warmup):
    if warmup <= 0:
        return start
    try:
        index = dates.index(start)
    except ValueError:
        return start
    return dates[max(index - warmup, 0)]


# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--strategy", default="sma_cross")
        p.add_argument("--synthetic", action="store_true")
        p.add_argument("--regime", default="mixed", choices=["mixed", "trend", "meanrev"])
        p.add_argument("--start", type=dt.date.fromisoformat, default=None)
        p.add_argument("--end", type=dt.date.fromisoformat, default=None)
        p.add_argument("--symbols", nargs="*", default=None)
        p.add_argument("--cash", type=float, default=300_000.0)
        p.add_argument(
            "--costs", default="realistic",
            choices=["optimistic", "realistic", "pessimistic"],
        )
        p.add_argument(
            "--objective", default="sharpe", choices=["sharpe", "t_stat", "total_return"]
        )
        p.add_argument("--minimum-trades", type=int, default=5)
        p.add_argument("--ignore-lot-size", action="store_true")
        p.add_argument("--lot-size", type=int, default=100, help="売買単位。少額なら1")
        p.add_argument(
            "--extra-spread", type=float, default=None,
            help="単元未満株の追加スプレッド（片道・比率）。省略時は --lot-size に応じた既定値",
        )

    p_holdout = sub.add_parser("holdout", help="前後に1回だけ分けて検証する")
    common(p_holdout)
    p_holdout.add_argument("--train-ratio", type=float, default=0.7)
    p_holdout.set_defaults(func=run_holdout)

    p_wf = sub.add_parser("walkforward", help="窓をずらしながら学習と検証を繰り返す")
    common(p_wf)
    p_wf.add_argument("--train", type=int, default=250, help="学習期間（営業日）")
    p_wf.add_argument("--test", type=int, default=60, help="検証期間（営業日）")
    p_wf.add_argument("--step", type=int, default=None, help="窓の進め幅（既定は検証期間と同じ）")
    p_wf.add_argument(
        "--anchored", action="store_true", help="学習の開始点を固定して窓を伸ばす"
    )
    p_wf.set_defaults(func=run_walkforward)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
