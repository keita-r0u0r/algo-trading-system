"""FX の日中売買を、GMOコインの実際の1分足で検証する。

ダミーデータ（``fx_research.py``）で仕組みを確かめたあとの、本番の検証。

  python scripts/fx_real.py --fetch          # 足を取り込んでから検証（取得済みの日は飛ばす）
  python scripts/fx_real.py                  # 取り込み済みの足で検証
  python scripts/fx_real.py --run-lockbox    # 最良の候補を、取り置いた期間で一度だけ評価する

## 条件（GMOコイン 外国為替FX の API 取引）

  * 1万円で始め、**1か月ごとに** 資金を戻して評価する（目的は「その月に資産が増えるか」）
  * 1回 100通貨（API の新規注文の最小）。新規は20倍・維持は25倍まで。証拠金維持率100%でロスカット
  * スプレッドは足ごとの実測（始値の時点の買値 − 売値）。API の手数料 0.002% と、成行・逆指値のずれを足す
  * 日をまたがない（営業日の終わりに手仕舞う）

## 偶然の当たりを拾わないために

  * 最後の ``--lockbox-months`` か月は取り置き、候補を絞るまで見ない
  * 候補の数だけ試行回数 N に数え、「N 回試せば偶然でも出る t 値」と比べる（``state/experiments.jsonl`` に追記）
  * ランダムに入る戦略を何通りも走らせ、その分布と比べる
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from jqbt.application.fx_backtest import FxConfig, run_fx  # noqa: E402
from jqbt.core.fx.model import FxCosts, FxInstrument  # noqa: E402
from jqbt.core.selection import expected_max_sharpe  # noqa: E402
from jqbt.infrastructure import experiment_log  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoFxClient  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import FIRST_DAY, fetch_range, load  # noqa: E402
from jqbt.strategies.fx import BollingerRevert, EmaCross, RandomFx, RangeBreakout  # noqa: E402

#: 試す候補。ここに足した数だけ試行回数 N が増える
CANDIDATES = {
    "ema_cross_5_20": lambda: EmaCross(5, 20),
    "ema_cross_10_40": lambda: EmaCross(10, 40),
    "bollinger_20_2": lambda: BollingerRevert(20, 2.0),
    "bollinger_60_2.5": lambda: BollingerRevert(60, 2.5),
    "breakout_30": lambda: RangeBreakout(30),
    "breakout_120": lambda: RangeBreakout(120, stop_pips=15, take_pips=30),
}


def gmo_config(args) -> FxConfig:
    inst = FxInstrument(symbol=args.symbol, min_units=100, leverage=25, losscut_ratio=1.0, entry_leverage=20)
    costs = FxCosts(slippage_pips=args.slippage, stop_slippage_pips=args.stop_slippage, fee_rate=0.00002, losscut_fee=0.05)
    return FxConfig(initial_cash=args.cash, instrument=inst, costs=costs, units=args.units,
                    max_entry_spread_pips=args.max_entry_spread)


def by_month(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    month = pd.Series([f"{d:%Y-%m}" for d in bars["session"]], index=bars.index)
    return {m: bars[month.to_numpy() == m] for m in sorted(month.unique())}


def run_months(months: dict[str, pd.DataFrame], make, config: FxConfig) -> dict:
    """月ごとに資金を戻して走らせる。"""
    returns, pnls, trades, losscuts, cost = [], [], 0, 0, 0.0
    for bars in months.values():
        result = run_fx(bars, make(), config)
        m = result.summary
        returns.append(m["total_return"])
        pnls.extend(t.pnl for t in result.trades)
        trades += m["trades"]
        cost += m["total_cost"]
        losscuts += int(m["losscut"])
    a = np.array(pnls)
    t = float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))) if len(a) > 1 and a.std(ddof=1) > 0 else None
    return {
        "returns": returns,
        "mean_return": float(np.mean(returns)),
        "worst_month": float(np.min(returns)),
        "positive_months": sum(r > 0 for r in returns),
        "months": len(returns),
        "trades": trades,
        "expectancy": float(a.mean()) if len(a) else None,
        "t": t,
        "losscut_months": losscuts,
        "cost": cost,
    }


def spread_profile(bars: pd.DataFrame) -> None:
    hours = bars.index.hour
    print("\n【スプレッドの実測】時刻（日本時間）ごと、始値の時点の買値 − 売値（pips）")
    print(f"  全体: 中央値 {bars['spread'].median():.2f} / 90% 点 {bars['spread'].quantile(0.9):.2f}")
    line = []
    for h in range(24):
        s = bars["spread"][hours == h]
        if len(s):
            line.append(f"{h:02d}時 {s.median():.1f}/{s.quantile(0.9):.1f}")
    for k in range(0, len(line), 6):
        print("  " + "   ".join(line[k : k + 6]))
    print("  （中央値/90% 点。朝の切り替えの時間は広がる）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="USD_JPY")
    parser.add_argument("--fetch", action="store_true", help="先に足を取り込む（取得済みの日は飛ばす）")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--units", type=float, default=100)
    parser.add_argument("--slippage", type=float, default=0.1, help="成行の不利なずれ（pips）")
    parser.add_argument("--stop-slippage", type=float, default=0.3, help="逆指値の不利なずれ（pips）")
    parser.add_argument("--max-entry-spread", type=float, default=None,
                        help="スプレッドがこれ（pips）を超える足では新規に入らない。変えると別の候補として試行回数に数える")
    parser.add_argument("--lockbox-months", type=int, default=3)
    parser.add_argument("--random-seeds", type=int, default=20)
    parser.add_argument("--run-lockbox", action="store_true")
    args = parser.parse_args()

    if args.fetch:
        print(f"{args.symbol} の1分足を取り込みます（{FIRST_DAY} から。取得済みの日は飛ばす）")
        fetch_range(GmoFxClient(), args.symbol, FIRST_DAY, dt.date.today())

    bars = load(args.symbol)
    months = by_month(bars)
    current = f"{dt.date.today():%Y-%m}"
    months.pop(current, None)  # 終わっていない月は使わない
    names = list(months)
    if len(names) <= args.lockbox_months + 3:
        print(f"月が足りません（{len(names)} か月）")
        return 1
    dev = {m: months[m] for m in names[: -args.lockbox_months]}
    lock = {m: months[m] for m in names[-args.lockbox_months :]}
    data_end = str(bars["session"].iloc[-1])
    config = gmo_config(args)
    print(f"{args.symbol} / 資金 {args.cash:,.0f}円を毎月戻す / 1回 {args.units:,.0f}通貨 / API 手数料 0.002% / ロスカット 100%")
    print(f"開発期間 {names[0]} 〜 {list(dev)[-1]}（{len(dev)} か月）/ "
          f"取り置き {list(lock)[0]} 〜 {names[-1]}（{len(lock)} か月・{'使用済み' if experiment_log.lockbox_used(data_end) else '未使用'}）")

    dev_bars = pd.concat(dev.values())
    spread_profile(dev_bars)

    print(f"\nランダムに入る戦略を {args.random_seeds} 通り走らせています…", flush=True)
    random_runs = [run_months(dev, lambda s=s: RandomFx(seed=s), config) for s in range(args.random_seeds)]
    random_means = np.array([r["mean_return"] for r in random_runs])

    suffix = f"_spread{args.max_entry_spread:g}" if args.max_entry_spread is not None else ""
    # 条件を変えて試した分も、同じデータでの試行回数に数える
    earlier = {r["label"] for r in experiment_log.load() if r.get("kind") == "fx_dev" and r.get("data_end") == data_end}
    trials = len(earlier | {name + suffix for name in CANDIDATES})
    rows = {}
    for name, make in CANDIDATES.items():
        print(f"  {name} …", flush=True)
        rows[name] = r = run_months(dev, make, config)
        r["cost_per_trade"] = r.pop("cost") / r["trades"] if r["trades"] else None
        r["random_pct"] = float((random_means < r["mean_return"]).mean() * 100)
        experiment_log.append({
            "kind": "fx_dev", "strategy": name, "label": name + suffix, "symbol": args.symbol,
            "max_entry_spread": args.max_entry_spread,
            "window": [names[0], list(dev)[-1]], "data_end": data_end, "cash": args.cash, "units": args.units,
            "mean_monthly_return": r["mean_return"], "positive_months": r["positive_months"], "months": r["months"],
            "trades": r["trades"], "expectancy": r["expectancy"], "t_stat": r["t"], "losscut_months": r["losscut_months"],
        })

    print(f"\n【開発期間の成績】毎月1万円から。このデータでの試行回数 {trials} 通り")
    print(f"  {'戦略':<18}{'月の平均':>8}{'増えた月':>9}{'最悪の月':>9}{'取引/月':>8}{'1取引の期待値':>12}{'t値':>7}{'ロスカット':>8}{'ランダム比':>9}")
    for name, r in rows.items():
        t = f"{r['t']:+.2f}" if r["t"] is not None else "—"
        exp = f"{r['expectancy']:+.2f}円" if r["expectancy"] is not None else "—"
        print(f"  {name:<18}{r['mean_return']:>+8.2%}{r['positive_months']:>5}/{r['months']:<3}{r['worst_month']:>+9.1%}"
              f"{r['trades'] / r['months']:>8.0f}{exp:>12}{t:>7}{r['losscut_months']:>8}{r['random_pct']:>8.0f}%")
    print("  1取引あたりのコスト（スプレッド・ずれ・手数料）: " + " / ".join(
        f"{k} {r['cost_per_trade']:.2f}円" for k, r in rows.items() if r["cost_per_trade"] is not None))
    lo, mid, hi = np.percentile(random_means, [5, 50, 95])
    print(f"  {'ランダム（中央値）':<16}{mid:>+8.2%}   （5〜95%: {lo:+.2%} 〜 {hi:+.2%}）")

    best_name = max(rows, key=lambda k: rows[k]["t"] if rows[k]["t"] is not None else -math.inf)
    best = rows[best_name]
    expected = expected_max_sharpe(trials, max(best["trades"], 2))
    needed_t = max(2.0, (expected or 0) * math.sqrt(max(best["trades"], 2)))
    print(f"\n【判定】t 値が最も高いのは {best_name}（t = {best['t'] or 0:+.2f}）。"
          f"{trials} 通り試せば偶然でも出る t 値は約 {needed_t:.2f}")
    survived = (best["t"] or 0) >= needed_t and best["random_pct"] >= 95
    if survived:
        print("  → 開発期間では否定されていません。取り置いた期間（--run-lockbox）で一度だけ確かめます")
    else:
        print("  → 優位性があるとは言えません（偶然の範囲、またはランダムと区別がつかない）")

    if not args.run_lockbox:
        return 0
    if experiment_log.lockbox_used(data_end):
        print("\nこのデータの取り置き期間は、もう評価に使いました。新しい月のデータが貯まるまで待ってください")
        return 1
    r = run_months(lock, CANDIDATES[best_name], config)
    experiment_log.append({"kind": "lockbox", "label": best_name, "symbol": args.symbol, "data_end": data_end,
                           "window": [list(lock)[0], names[-1]], "mean_monthly_return": r["mean_return"],
                           "positive_months": r["positive_months"], "t_stat": r["t"], "trades": r["trades"]})
    print(f"\n【取り置き期間】{best_name}: 月の平均 {r['mean_return']:+.2%} / 増えた月 {r['positive_months']}/{r['months']} / "
          f"t = {r['t'] or 0:+.2f}（この期間はもう使えません）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
