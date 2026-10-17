"""バックテストを1本回す。

    # 合成データ（認証不要・成績に意味はない・仕組みの確認用）
    python scripts/run.py --strategy sma_cross --synthetic

    # ベースラインと比較する（これを先にやること）
    python scripts/run.py --compare --synthetic

    # 実データ（scripts/fetch.py で取り込み済みの前提）
    python scripts/run.py --strategy sma_cross --start 2023-01-01 --end 2024-12-31

    # コスト前提の感度を見る
    python scripts/run.py --strategy sma_cross --synthetic --costs all
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.infrastructure.env import load_env

load_env(Path(__file__).resolve().parent)

import numpy as np
import pandas as pd

from jqbt import REALISTIC, compute, format_report
from jqbt.interfaces.composition import (
    COST_MODELS,
    DataRequest,
    build_runner,
    default_extra_spread,
)
from jqbt.interfaces.composition import load_panel as _load
from jqbt.strategies import BuyAndHold, RandomEntry, SmaCross


def load_panel(args: argparse.Namespace) -> pd.DataFrame:
    if args.synthetic:
        print("合成データを使用します。**成績に意味はありません**（仕組みの確認用）\n")
    return _load(
        DataRequest(
            synthetic=args.synthetic,
            start=args.start,
            end=args.end,
            symbols=args.symbols,
            regime=args.regime,
        )
    )


def build_strategy(name: str, args: argparse.Namespace):
    if name == "sma_cross":
        return SmaCross(fast=args.fast, slow=args.slow, max_positions=args.max_positions)
    if name == "buy_and_hold":
        return BuyAndHold(max_positions=args.max_positions)
    if name == "random":
        return RandomEntry(max_positions=args.max_positions, seed=args.seed)
    raise SystemExit(f"未知の戦略: {name}")


def run_one(panel, strategy, args, costs=REALISTIC):
    return build_runner(
        panel,
        args.cash,
        costs,
        not args.ignore_lot_size,
        args.lot_size,
        args.extra_spread,
    ).run(strategy)


# ----------------------------------------------------------------------
def compare(panel, args) -> None:
    """戦略・買って持つだけ・ランダム群の3者を並べる。

    ランダムは seed を変えて複数回まわし、成績の分布を出す。
    **戦略の Sharpe がその分布の内側なら、優位性は示せていない。**
    """
    rows = []

    strategy = build_strategy(args.strategy, args)
    result = run_one(panel, strategy, args)
    metrics = compute(result)
    rows.append(("戦略: " + strategy.name, metrics))

    hold = run_one(panel, BuyAndHold(max_positions=args.max_positions), args)
    rows.append(("買って持つだけ", compute(hold)))

    print(f"ランダム売買を {args.random_runs} 回まわしています…")
    random_metrics = []
    for seed in range(args.random_runs):
        outcome = run_one(
            panel,
            RandomEntry(
                holding_days=args.slow,
                max_positions=args.max_positions,
                seed=seed,
            ),
            args,
        )
        try:
            random_metrics.append(compute(outcome))
        except ValueError:
            continue

    print()
    header = f"{'':<22}{'総リターン':>12}{'CAGR':>10}{'最大DD':>10}{'Sharpe':>9}{'取引数':>8}"
    print(header)
    print("-" * len(header))
    for label, m in rows:
        print(
            f"{label:<22}"
            f"{_pct(m.total_return):>12}"
            f"{_pct(m.cagr):>10}"
            f"{_pct(m.max_drawdown):>10}"
            f"{_num(m.sharpe):>9}"
            f"{m.trade_count:>8}"
        )

    if random_metrics:
        returns = np.array([m.total_return for m in random_metrics])
        sharpes = np.array([m.sharpe for m in random_metrics if m.sharpe is not None])
        print(
            f"{'ランダム（中央値）':<20}"
            f"{_pct(float(np.median(returns))):>12}"
            f"{'':>10}{'':>10}"
            f"{_num(float(np.median(sharpes)) if sharpes.size else None):>9}"
        )
        print(
            f"{'ランダム（5-95%）':<20}"
            f"{_pct(float(np.percentile(returns, 5))):>12}"
            f" 〜 {_pct(float(np.percentile(returns, 95)))}"
        )

        edge = rows[0][1]
        if sharpes.size and edge.sharpe is not None:
            percentile = float((sharpes < edge.sharpe).mean() * 100)
            print()
            print(f"戦略の Sharpe は、ランダム分布の {percentile:.0f} パーセンタイルです。")
            if percentile < 95:
                print(
                    "  → ランダムと区別がつきません。"
                    "この戦略に優位性があるとは言えません。"
                )
            else:
                print(
                    "  → ランダムより上に出ていますが、"
                    "パラメータを何通り試したかを思い出してください。"
                )
                print(
                    "     20通り試せば、偶然1つは95パーセンタイルに入ります（多重検定）。"
                )

    print()
    print(format_report(rows[0][1], f"{rows[0][0]} の詳細"))


def _pct(value):
    return "—" if value is None else f"{value * 100:+.2f}%"


def _num(value):
    return "—" if value is None else f"{value:.2f}"


# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="sma_cross")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--regime", default="mixed", choices=["mixed", "trend", "meanrev"])
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--cash", type=float, default=300_000.0)
    parser.add_argument("--fast", type=int, default=25)
    parser.add_argument("--slow", type=int, default=75)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--random-runs", type=int, default=30)
    parser.add_argument("--compare", action="store_true", help="ベースラインと比較する")
    parser.add_argument(
        "--costs", default="realistic", choices=[*COST_MODELS, "all"]
    )
    parser.add_argument(
        "--ignore-lot-size",
        action="store_true",
        help="単元株を無視する（理論上の優位性だけを見たいとき）",
    )
    parser.add_argument(
        "--lot-size",
        type=int,
        default=100,
        help="売買単位。既定は100株。1万円など少額なら1（単元未満株）",
    )
    parser.add_argument(
        "--extra-spread",
        type=float,
        default=None,
        help="単元未満株の追加スプレッド（片道・比率）。省略時は --lot-size に応じた既定値",
    )
    args = parser.parse_args()

    spread = default_extra_spread(args.lot_size) if args.extra_spread is None else args.extra_spread
    if spread:
        print(
            f"売買単位 {args.lot_size}株 / 追加スプレッド 片道{spread:.2%}"
            "（仮定。証券会社の実測値に置き換えること）\n"
        )

    panel = load_panel(args)
    days = len(set(panel.index.get_level_values("date")))
    symbols = len(set(panel.index.get_level_values("symbol")))
    print(f"パネル: {days}営業日 × {symbols}銘柄 = {len(panel):,}行\n")

    from jqbt.infrastructure.parquet_store import capital_feasibility, format_feasibility

    feasibility = capital_feasibility(panel, args.cash, args.max_positions, args.lot_size)
    if feasibility and not args.ignore_lot_size:
        print(format_feasibility(feasibility))
        print()

    if args.compare:
        compare(panel, args)
        return 0

    strategy = build_strategy(args.strategy, args)
    models = COST_MODELS if args.costs == "all" else {args.costs: COST_MODELS[args.costs]}

    for name, costs in models.items():
        result = run_one(panel, strategy, args, costs)
        try:
            metrics = compute(result)
        except ValueError as exc:
            print(f"[{name}] {exc}")
            continue
        print(format_report(metrics, f"{strategy.name} / コスト前提: {name}"))
        print()

    if len(models) > 1:
        print(
            "コスト前提を変えて結論が反転するなら、"
            "その戦略は優位性ではなくコスト仮定に依存しています。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
