"""FX の日中売買を、GMOコインの実際の1分足で検証する。

ダミーデータ（``fx_research.py``）で仕組みを確かめたあとの、本番の検証。

  python scripts/fx_real.py --fetch            # 足を取り込んでから検証（取得済みの日は飛ばす）
  python scripts/fx_real.py                    # 取引の少ない候補（low_freq）を検証
  python scripts/fx_real.py --set intraday     # 1分足で頻繁に売買する候補（コストに負けると分かったもの）
  python scripts/fx_real.py --run-lockbox      # 最良の候補を、取り置いた期間で一度だけ評価する
  python scripts/fx_real.py --risk all         # リスクの度合い（小・中・最大）で並べる。一切売買しない場合も

## 条件（GMOコイン 外国為替FX の API 取引）

  * 1万円で始め、**1か月ごとに** 資金を戻して評価する（目的は「その月に資産が増えるか」）
  * 1回 100通貨（API の新規注文の最小）。新規は20倍・維持は25倍まで。証拠金維持率100%でロスカット
  * スプレッドは足ごとの実測（始値の時点の買値 − 売値）。API の手数料 0.002% と、成行・逆指値のずれを足す
  * 日をまたがない（営業日の終わりに手仕舞う）。日をまたぐ候補だけ、スワップ（**仮定**）を数える

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
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from jqbt.application.fx_backtest import FxConfig, run_fx  # noqa: E402
from jqbt.core.fx.model import RISK_PROFILES, FxCosts, FxInstrument  # noqa: E402
from jqbt.core.selection import expected_max_sharpe  # noqa: E402
from jqbt.infrastructure import experiment_log  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoFxClient  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import FIRST_DAY, fetch_range, load  # noqa: E402
from jqbt.strategies.fx import (  # noqa: E402
    BollingerRevert,
    BuyAndHoldFx,
    EmaCross,
    GotobiFix,
    HourlyBreakout,
    LondonBreakout,
    NoTradeFx,
    RandomFx,
    RangeBreakout,
    RateMomentum,
)
from jqbt.infrastructure import rates  # noqa: E402

#: 試す候補（名前 → (作り方, 日をまたぐか)）。ここに足した数だけ試行回数 N が増える
CANDIDATE_SETS = {
    # 1分足で頻繁に売買する。コスト（往復約1 pip）に負けると分かった（2026-10-21）
    "intraday": {
        "ema_cross_5_20": (lambda: EmaCross(5, 20), False),
        "ema_cross_10_40": (lambda: EmaCross(10, 40), False),
        "bollinger_20_2": (lambda: BollingerRevert(20, 2.0), False),
        "bollinger_60_2.5": (lambda: BollingerRevert(60, 2.5), False),
        "breakout_30": (lambda: RangeBreakout(30), False),
        "breakout_120": (lambda: RangeBreakout(120, stop_pips=15, take_pips=30), False),
    },
    # 取引を少なくし、1回で 1 pip を大きく超える値幅を狙う。理由の説明がつくものだけ
    "low_freq": {
        "gotobi_fix": (GotobiFix, False),
        "london_breakout": (LondonBreakout, False),
        "hourly_breakout_20": (HourlyBreakout, True),
    },
}
#: 比べる基準（試行回数に数えない）
BASELINES = {
    "intraday": {"no_trade": (NoTradeFx, False)},
    "low_freq": {"no_trade": (NoTradeFx, False), "buy_and_hold": (BuyAndHoldFx, True)},
}
#: ランダムに入る戦略。候補と同じくらいの頻度にする
RANDOM = {
    "intraday": lambda s: RandomFx(seed=s),
    "low_freq": lambda s: RandomFx(probability=1 / 1440, stop_pips=20, take_pips=40, seed=s),
}
#: スプレッドの広い足（朝の切り替え）では入らない。low_freq は最初からこの条件で決めた
DEFAULT_MAX_SPREAD = {"intraday": None, "low_freq": 1.0}


def gmo_config(args) -> FxConfig:
    inst = FxInstrument(symbol=args.symbol, min_units=100, leverage=25, losscut_ratio=1.0, entry_leverage=20)
    costs = FxCosts(slippage_pips=args.slippage, stop_slippage_pips=args.stop_slippage, fee_rate=0.00002, losscut_fee=0.05)
    return FxConfig(initial_cash=args.cash, instrument=inst, costs=costs, units=args.units,
                    max_entry_spread_pips=args.max_entry_spread, swap_long=args.swap_long, swap_short=args.swap_short)


def by_month(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    month = pd.Series([f"{d:%Y-%m}" for d in bars["session"]], index=bars.index)
    return {m: bars[month.to_numpy() == m] for m in sorted(month.unique())}


def run_months(months: dict[str, pd.DataFrame], make, config: FxConfig, overnight: bool = False,
               warmup: dict[str, pd.DataFrame] | None = None) -> dict:
    """月ごとに資金を戻して走らせる。日をまたぐ候補も、月の終わりには手仕舞う。

    ``warmup`` があれば、各月の前に「助走」の足（前の月の終わり）を戦略に見せてから始める（売買はしない）。
    """
    config = replace(config, flat_at_day_end=not overnight)
    returns, pnls, trades, losscuts, cost, units = [], [], 0, 0, 0.0, []
    for month, bars in months.items():
        if warmup and month in warmup and not warmup[month].empty:
            run_config = replace(config, trade_from=bars.index[0])
            bars = pd.concat([warmup[month], bars])
        else:
            run_config = config
        result = run_fx(bars, make(), run_config)
        m = result.summary
        returns.append(m["total_return"])
        pnls.extend(t.pnl for t in result.trades)
        units.extend(abs(t.units) for t in result.trades)
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
        "units": float(np.mean(units)) if units else None,
        "pnls": pnls,
    }


def compare_risk(args, dev: dict, config: FxConfig, candidates: dict, data_end: str) -> int:
    """同じ売買の判断を、リスクの度合いだけ変えて並べる。

    判断は同じなので、優位性の有無（t 値の符号）は変わらない。変わるのは月の損益の振れ幅、
    ロスカットで終わる月、コストの額。目的（その月に資産が増える確率）にどう効くかを見る。
    """
    profiles = list(RISK_PROFILES) if args.risk == "all" else [args.risk]
    rows = {**candidates, **BASELINES.get(args.set, {})}
    print("\n【リスクの度合い】" + " / ".join(
        f"{p}: 1回の損切りで資金の{RISK_PROFILES[p].risk_per_trade:.1%}、{RISK_PROFILES[p].max_leverage:g}倍まで、"
        f"1日 −{RISK_PROFILES[p].daily_loss_limit:.0%} で停止" for p in profiles))
    print(f"  {'戦略':<20}{'度合い':<8}{'月の平均':>8}{'増えた月':>9}{'最悪の月':>9}{'最良の月':>9}{'ロスカット':>8}{'取引/月':>8}{'数量の平均':>10}")
    for name, (make, overnight) in rows.items():
        for profile in profiles:
            r = run_months(dev, make, replace(config, risk=RISK_PROFILES[profile]), overnight)
            best = max(r["returns"])
            print(f"  {name:<20}{profile:<8}{r['mean_return']:>+8.2%}{r['positive_months']:>5}/{r['months']:<3}"
                  f"{r['worst_month']:>+9.1%}{best:>+9.1%}{r['losscut_months']:>8}{r['trades'] / r['months']:>8.0f}"
                  f"{(r['units'] or 0):>10,.0f}")
            if name not in BASELINES.get(args.set, {}):
                experiment_log.append({
                    "kind": "fx_risk", "label": f"{name}_{profile}", "strategy": name, "risk": profile,
                    "symbol": args.symbol, "data_end": data_end, "mean_monthly_return": r["mean_return"],
                    "positive_months": r["positive_months"], "months": r["months"], "losscut_months": r["losscut_months"],
                })
    print("  ※ 判断は同じで、数量だけが違う（試行回数には数えない）。no_trade は一切売買しない基準")
    return 0


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
    parser.add_argument("--fetch", action="store_true", help="先に足と日米の金利を取り込む（取得済みの日は飛ばす）")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--units", type=float, default=100)
    parser.add_argument("--slippage", type=float, default=0.1, help="成行の不利なずれ（pips）")
    parser.add_argument("--stop-slippage", type=float, default=0.3, help="逆指値の不利なずれ（pips）")
    parser.add_argument("--set", choices=list(CANDIDATE_SETS), default="low_freq", help="試す候補の組")
    parser.add_argument("--max-entry-spread", type=float, default=None,
                        help="スプレッドがこれ（pips）を超える足では新規に入らない（既定は組ごと）。変えると別の候補として試行回数に数える")
    parser.add_argument("--swap-long", type=float, default=0.012,
                        help="買いのスワップ（円/通貨/日）。**仮定**（日米の金利差 約3%%）")
    parser.add_argument("--swap-short", type=float, default=-0.013, help="売りのスワップ（円/通貨/日）。**仮定**")
    parser.add_argument("--lockbox-months", type=int, default=3)
    parser.add_argument("--random-seeds", type=int, default=20)
    parser.add_argument("--run-lockbox", action="store_true")
    parser.add_argument("--risk", choices=["all", *RISK_PROFILES], default=None,
                        help="リスクの度合いで数量を決めて並べる（売買の判断は同じなので、試行回数には数えない）")
    args = parser.parse_args()
    candidates = CANDIDATE_SETS[args.set]
    explicit_spread = args.max_entry_spread is not None
    if not explicit_spread:
        args.max_entry_spread = DEFAULT_MAX_SPREAD[args.set]

    if args.fetch:
        print(f"{args.symbol} の1分足を取り込みます（{FIRST_DAY} から。取得済みの日は飛ばす）")
        fetch_range(GmoFxClient(), args.symbol, FIRST_DAY, dt.date.today())
        print("日米の国債利回りを取り込みます（財務省・FRED）")
        rates.fetch()
    if args.set == "low_freq":
        # 金利差のルールは、取り込んだ金利が要る
        spread = rates.spread()
        candidates = {**candidates, "rate_momentum_20": (lambda: RateMomentum(spread), True)}

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

    if args.risk:
        return compare_risk(args, dev, config, candidates, data_end)

    dev_bars = pd.concat(dev.values())
    spread_profile(dev_bars)

    print(f"\nランダムに入る戦略を {args.random_seeds} 通り走らせています…", flush=True)
    random_runs = [run_months(dev, lambda s=s: RANDOM[args.set](s), config) for s in range(args.random_seeds)]
    random_means = np.array([r["mean_return"] for r in random_runs])

    suffix = f"_spread{args.max_entry_spread:g}" if explicit_spread and args.max_entry_spread is not None else ""
    # 条件を変えて試した分も、同じデータでの試行回数に数える
    earlier = {r["label"] for r in experiment_log.load() if r.get("kind") == "fx_dev" and r.get("data_end") == data_end}
    trials = len(earlier | {name + suffix for name in candidates})
    rows = {}
    for name, (make, overnight) in candidates.items():
        print(f"  {name} …", flush=True)
        rows[name] = r = run_months(dev, make, config, overnight)
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
    for name, (make, overnight) in BASELINES.get(args.set, {}).items():
        b = run_months(dev, make, config, overnight)
        print(f"  {name + '（基準）':<16}{b['mean_return']:>+8.2%}{b['positive_months']:>5}/{b['months']:<3}{b['worst_month']:>+9.1%}"
              f"   （スワップ 買い {args.swap_long:+.3f}円/通貨/日 の仮定）")

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
    make, overnight = candidates[best_name]
    r = run_months(lock, make, config, overnight)
    experiment_log.append({"kind": "lockbox", "label": best_name, "symbol": args.symbol, "data_end": data_end,
                           "window": [list(lock)[0], names[-1]], "mean_monthly_return": r["mean_return"],
                           "positive_months": r["positive_months"], "t_stat": r["t"], "trades": r["trades"]})
    print(f"\n【取り置き期間】{best_name}: 月の平均 {r['mean_return']:+.2%} / 増えた月 {r['positive_months']}/{r['months']} / "
          f"t = {r['t'] or 0:+.2f}（この期間はもう使えません）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
